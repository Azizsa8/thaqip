#!/usr/bin/env python3
"""
Thaqip Agent Fleet Manager Daemon
Always-running service that manages agent fleet operations via API.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Add project root to path
sys.path.insert(0, "/home/ais04/thaqip")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("/home/ais04/thaqip/agent-fleet/var/fleet-daemon.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("thaqip.fleet")


@dataclass
class AgentProfile:
    name: str
    title: str
    description: str
    model: str
    provider: str = "openrouter"
    skills: list[str] = field(default_factory=list)
    toolsets: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    system_prompt_additions: str = ""


@dataclass
class OperationTemplate:
    id: str
    name: str
    description: str
    trigger: str
    schedule: str | None = None
    agents: list[dict] = field(default_factory=list)
    aggregation: str = ""
    timeout_seconds: int = 1800
    retry_policy: dict = field(default_factory=lambda: {"max_attempts": 2, "backoff_seconds": 30})


@dataclass
class OperationInstance:
    id: str
    template_id: str
    status: str  # pending, running, completed, failed, cancelled
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    trigger_data: dict = field(default_factory=dict)
    agent_results: dict = field(default_factory=dict)
    final_result: dict | None = None
    error: str | None = None
    retry_count: int = 0


class FleetConfig:
    def __init__(self, config_path: str):
        with open(config_path) as f:
            self.raw = yaml.safe_load(f)
        self.fleet = self.raw.get("fleet", {})
        self.profiles = self._load_profiles()
        self.operations = self._load_operations()

    def _load_profiles(self) -> dict[str, AgentProfile]:
        profiles = {}
        for p in self.fleet.get("profiles", []):
            profiles[p["name"]] = AgentProfile(**p)
        return profiles

    def _load_operations(self) -> dict[str, OperationTemplate]:
        ops = {}
        for o in self.fleet.get("operations", []):
            ops[o["id"]] = OperationTemplate(**o)
        return ops


class FleetDatabase:
    """Simple JSON file database for operation tracking (replace with Postgres later)."""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.db_path.exists():
            self._write({"operations": {}, "results": {}})

    def _read(self) -> dict:
        with open(self.db_path) as f:
            return json.load(f)

    def _write(self, data: dict):
        with open(self.db_path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def create_operation(self, op: OperationInstance) -> OperationInstance:
        data = self._read()
        data["operations"][op.id] = self._op_to_dict(op)
        self._write(data)
        return op

    def update_operation(self, op: OperationInstance):
        data = self._read()
        data["operations"][op.id] = self._op_to_dict(op)
        self._write(data)

    def get_operation(self, op_id: str) -> OperationInstance | None:
        data = self._read()
        op_data = data["operations"].get(op_id)
        if not op_data:
            return None
        return self._dict_to_op(op_data)

    def list_operations(self, limit: int = 100, status: str | None = None) -> list[OperationInstance]:
        data = self._read()
        ops = []
        for op_data in data["operations"].values():
            op = self._dict_to_op(op_data)
            if status is None or op.status == status:
                ops.append(op)
        ops.sort(key=lambda x: x.created_at, reverse=True)
        return ops[:limit]

    def _op_to_dict(self, op: OperationInstance) -> dict:
        return {
            "id": op.id,
            "template_id": op.template_id,
            "status": op.status,
            "created_at": op.created_at.isoformat(),
            "started_at": op.started_at.isoformat() if op.started_at else None,
            "completed_at": op.completed_at.isoformat() if op.completed_at else None,
            "trigger_data": op.trigger_data,
            "agent_results": op.agent_results,
            "final_result": op.final_result,
            "error": op.error,
            "retry_count": op.retry_count,
        }

    def _dict_to_op(self, d: dict) -> OperationInstance:
        return OperationInstance(
            id=d["id"],
            template_id=d["template_id"],
            status=d["status"],
            created_at=datetime.fromisoformat(d["created_at"]),
            started_at=datetime.fromisoformat(d["started_at"]) if d["started_at"] else None,
            completed_at=datetime.fromisoformat(d["completed_at"]) if d["completed_at"] else None,
            trigger_data=d["trigger_data"],
            agent_results=d["agent_results"],
            final_result=d["final_result"],
            error=d["error"],
            retry_count=d["retry_count"],
        )


class HermesAgentRunner:
    """Runs Hermes agents for fleet operations."""

    def __init__(self, fleet_home: str, workdir: str):
        self.fleet_home = Path(fleet_home)
        self.workdir = Path(workdir)
        self.profiles_dir = self.fleet_home / "profiles"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)

    async def run_agent(
        self,
        profile: AgentProfile,
        task: str,
        operation_id: str,
        agent_role: str,
        timeout: int = 1800,
    ) -> dict:
        """Run a Hermes agent with the given profile and task."""
        
        # Create profile directory if needed
        profile_dir = self.profiles_dir / profile.name
        profile_dir.mkdir(exist_ok=True)
        
        # Write profile config
        toolsets_str = "\n  - ".join(profile.toolsets)
        skills_str = "\n    - ".join(profile.skills)
        system_prompt_indented = self._indent(profile.system_prompt_additions, 4)
        
        config_yaml = profile_dir / "config.yaml"
        config_content = f"""model:
  model: {profile.model}
  provider: {profile.provider}
toolsets:
  - {toolsets_str}
skills:
  enabled:
    - {skills_str}
mcp:
  servers: {profile.mcp_servers}
agent:
  system_prompt_additions: |
{system_prompt_indented}
"""
        config_yaml.write_text(config_content)
        
        # Write SOUL.md
        soul_md = profile_dir / "SOUL.md"
        soul_content = f"""# {profile.title}

{profile.description}

## Role: {agent_role}

{profile.system_prompt_additions}
"""
        soul_md.write_text(soul_content)

        # Prepare Hermes command
        # Hermes profiles live at $HERMES_HOME/profiles/<name>/
        # We set HERMES_HOME to fleet_home so it uses our profile directory
        # Use `hermes chat -q` for one-shot query mode
        hermes_cmd = [
            "hermes",
            "chat",
            "-p", profile.name,
            "-q", task,
            "--max-turns", "20",
            "--quiet",  # Suppress banner/spinner for programmatic use
        ]
        
        env = os.environ.copy()
        env["HERMES_HOME"] = str(self.fleet_home)
        # Ensure API keys are passed through
        if "OPENROUTER_API_KEY" in os.environ:
            env["OPENROUTER_API_KEY"] = os.environ["OPENROUTER_API_KEY"]
        if "ANTHROPIC_API_KEY" in os.environ:
            env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
        if "THAQIP_ANTHROPIC_API_KEY" in os.environ:
            env["THAQIP_ANTHROPIC_API_KEY"] = os.environ["THAQIP_ANTHROPIC_API_KEY"]
        if "NVIDIA_API_KEY" in os.environ:
            env["NVIDIA_API_KEY"] = os.environ["NVIDIA_API_KEY"]
        
        log.info(f"Starting agent {profile.name} for operation {operation_id}")
        log.debug(f"Command: {' '.join(hermes_cmd)}")
        
        try:
            proc = await asyncio.create_subprocess_exec(
                *hermes_cmd,
                cwd=self.workdir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            
            result = {
                "profile": profile.name,
                "role": agent_role,
                "exit_code": proc.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "success": proc.returncode == 0,
                "completed_at": datetime.now(UTC).isoformat(),
            }
            
            if proc.returncode != 0:
                log.error(f"Agent {profile.name} failed: {result['stderr']}")
            else:
                log.info(f"Agent {profile.name} completed successfully")
                
            return result
            
        except asyncio.TimeoutError:
            log.error(f"Agent {profile.name} timed out after {timeout}s")
            return {
                "profile": profile.name,
                "role": agent_role,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Timeout after {timeout} seconds",
                "success": False,
                "completed_at": datetime.now(UTC).isoformat(),
            }
        except Exception as e:
            log.exception(f"Agent {profile.name} error: {e}")
            return {
                "profile": profile.name,
                "role": agent_role,
                "exit_code": -1,
                "stdout": "",
                "stderr": str(e),
                "success": False,
                "completed_at": datetime.now(UTC).isoformat(),
            }

    def _indent(self, text: str, spaces: int) -> str:
        return "\n".join(" " * spaces + line for line in text.splitlines())


class FleetManager:
    """Main fleet manager coordinating operations."""

    def __init__(self, config: FleetConfig):
        self.config = config
        self.db = FleetDatabase(
            "/home/ais04/thaqip/agent-fleet/var/fleet.db.json"
        )
        self.runner = HermesAgentRunner(
            config.fleet.get("home", "/home/ais04/thaqip/agent-fleet"),
            config.fleet.get("workdir", "/home/ais04/thaqip"),
        )
        self.running_operations: dict[str, asyncio.Task] = {}
        self.max_concurrent = config.fleet.get("monitoring", {}).get("max_concurrent_operations", 5)

    async def start_operation(
        self,
        template_id: str,
        trigger_data: dict | None = None,
        background: bool = True,
    ) -> OperationInstance:
        """Start an operation from a template."""
        
        template = self.config.operations.get(template_id)
        if not template:
            raise ValueError(f"Unknown operation template: {template_id}")

        # Check concurrency limit
        running = sum(1 for t in self.running_operations.values() if not t.done())
        if running >= self.max_concurrent:
            raise RuntimeError(f"Max concurrent operations ({self.max_concurrent}) reached")

        # Create operation instance
        op = OperationInstance(
            id=str(uuid.uuid4())[:8],
            template_id=template_id,
            status="pending",
            created_at=datetime.now(UTC),
            trigger_data=trigger_data or {},
        )
        
        self.db.create_operation(op)
        
        if background:
            task = asyncio.create_task(self._run_operation(op, template))
            self.running_operations[op.id] = task
        else:
            await self._run_operation(op, template)
            
        return op

    async def _run_operation(self, op: OperationInstance, template: OperationTemplate):
        """Execute an operation with all its agents."""
        
        op.status = "running"
        op.started_at = datetime.now(UTC)
        self.db.update_operation(op)
        
        log.info(f"Starting operation {op.id} ({template.name})")
        
        try:
            # Run each agent in sequence (could parallelize with dependencies)
            agent_outputs = {}
            
            for agent_spec in template.agents:
                profile_name = agent_spec["profile"]
                profile = self.config.profiles.get(profile_name)
                if not profile:
                    raise ValueError(f"Unknown profile: {profile_name}")
                
                # Render task template with trigger data
                task = agent_spec["task_template"].format(**op.trigger_data)
                
                # Run agent with retries
                max_attempts = template.retry_policy.get("max_attempts", 2)
                backoff = template.retry_policy.get("backoff_seconds", 30)
                
                for attempt in range(max_attempts):
                    result = await self.runner.run_agent(
                        profile=profile,
                        task=task,
                        operation_id=op.id,
                        agent_role=agent_spec.get("role", profile_name),
                        timeout=template.timeout_seconds,
                    )
                    
                    if result["success"]:
                        agent_outputs[profile_name] = result
                        break
                    elif attempt < max_attempts - 1:
                        log.warning(f"Agent {profile_name} failed (attempt {attempt+1}), retrying in {backoff}s")
                        await asyncio.sleep(backoff)
                    else:
                        agent_outputs[profile_name] = result
                        raise RuntimeError(f"Agent {profile_name} failed after {max_attempts} attempts: {result['stderr']}")
            
            # Run aggregation agent
            agg_profile = self.config.profiles.get(template.aggregation)
            if agg_profile:
                agent_outputs_summary = {k: v.get('stdout', '') for k, v in agent_outputs.items()}
                agg_task = f"""Synthesize the following agent outputs into a final deliverable for operation {template.name}:

Operation ID: {op.id}
Template: {template.name}
Trigger Data: {json.dumps(op.trigger_data, indent=2)}

Agent Outputs:
{json.dumps(agent_outputs_summary, indent=2)}

Produce a comprehensive final report as specified in the operation template."""
                
                agg_result = await self.runner.run_agent(
                    profile=agg_profile,
                    task=agg_task,
                    operation_id=op.id,
                    agent_role="aggregator",
                    timeout=600,
                )
                agent_outputs["aggregator"] = agg_result
                op.final_result = {"report": agg_result.get("stdout", ""), "sources": agent_outputs}
            
            op.agent_results = agent_outputs
            op.status = "completed"
            op.completed_at = datetime.now(UTC)
            
        except Exception as e:
            log.exception(f"Operation {op.id} failed: {e}")
            op.status = "failed"
            op.error = str(e)
            op.completed_at = datetime.now(UTC)
            
        finally:
            self.db.update_operation(op)
            self.running_operations.pop(op.id, None)
            log.info(f"Operation {op.id} finished with status: {op.status}")

    async def cancel_operation(self, op_id: str) -> bool:
        """Cancel a running operation."""
        task = self.running_operations.get(op_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            op = self.db.get_operation(op_id)
            if op:
                op.status = "cancelled"
                op.completed_at = datetime.now(UTC)
                self.db.update_operation(op)
            return True
        return False

    def get_operation(self, op_id: str) -> OperationInstance | None:
        return self.db.get_operation(op_id)

    def list_operations(self, limit: int = 100, status: str | None = None) -> list[OperationInstance]:
        return self.db.list_operations(limit, status)

    async def health_check(self) -> dict:
        """Health check endpoint."""
        running = sum(1 for t in self.running_operations.values() if not t.done())
        return {
            "status": "healthy",
            "running_operations": running,
            "max_concurrent": self.max_concurrent,
            "profiles_loaded": len(self.config.profiles),
            "operations_loaded": len(self.config.operations),
            "timestamp": datetime.now(UTC).isoformat(),
        }


# FastAPI app for API access
fleet_manager: FleetManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global fleet_manager
    config = FleetConfig("/home/ais04/thaqip/agent-fleet/config/fleet.yaml")
    fleet_manager = FleetManager(config)
    log.info("Fleet Manager started")
    yield
    log.info("Fleet Manager shutting down")


app = FastAPI(title="Thaqip Agent Fleet API", lifespan=lifespan)


class StartOperationRequest(BaseModel):
    template_id: str
    trigger_data: dict = Field(default_factory=dict)
    background: bool = True


class OperationResponse(BaseModel):
    id: str
    template_id: str
    status: str
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    trigger_data: dict
    final_result: dict | None = None
    error: str | None = None


@app.post("/operations", response_model=OperationResponse)
async def start_operation(request: StartOperationRequest, background_tasks: BackgroundTasks):
    """Start a new fleet operation."""
    if not fleet_manager:
        raise HTTPException(503, "Fleet manager not initialized")
    
    try:
        op = await fleet_manager.start_operation(
            request.template_id,
            request.trigger_data,
            request.background,
        )
        return OperationResponse(**{
            "id": op.id,
            "template_id": op.template_id,
            "status": op.status,
            "created_at": op.created_at.isoformat(),
            "started_at": op.started_at.isoformat() if op.started_at else None,
            "completed_at": op.completed_at.isoformat() if op.completed_at else None,
            "trigger_data": op.trigger_data,
            "final_result": op.final_result,
            "error": op.error,
        })
    except ValueError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(429, str(e))


@app.get("/operations", response_model=list[OperationResponse])
async def list_operations(limit: int = 100, status: str | None = None):
    """List operations with optional status filter."""
    if not fleet_manager:
        raise HTTPException(503, "Fleet manager not initialized")
    
    ops = fleet_manager.list_operations(limit, status)
    return [OperationResponse(**{
        "id": op.id,
        "template_id": op.template_id,
        "status": op.status,
        "created_at": op.created_at.isoformat(),
        "started_at": op.started_at.isoformat() if op.started_at else None,
        "completed_at": op.completed_at.isoformat() if op.completed_at else None,
        "trigger_data": op.trigger_data,
        "final_result": op.final_result,
        "error": op.error,
    }) for op in ops]


@app.get("/operations/{op_id}", response_model=OperationResponse)
async def get_operation(op_id: str):
    """Get operation details."""
    if not fleet_manager:
        raise HTTPException(503, "Fleet manager not initialized")
    
    op = fleet_manager.get_operation(op_id)
    if not op:
        raise HTTPException(404, "Operation not found")
    
    return OperationResponse(**{
        "id": op.id,
        "template_id": op.template_id,
        "status": op.status,
        "created_at": op.created_at.isoformat(),
        "started_at": op.started_at.isoformat() if op.started_at else None,
        "completed_at": op.completed_at.isoformat() if op.completed_at else None,
        "trigger_data": op.trigger_data,
        "final_result": op.final_result,
        "error": op.error,
    })


@app.post("/operations/{op_id}/cancel")
async def cancel_operation(op_id: str):
    """Cancel a running operation."""
    if not fleet_manager:
        raise HTTPException(503, "Fleet manager not initialized")
    
    success = await fleet_manager.cancel_operation(op_id)
    if not success:
        raise HTTPException(404, "Operation not found or not running")
    return {"status": "cancelled"}


@app.get("/health")
async def health():
    if not fleet_manager:
        return JSONResponse({"status": "initializing"}, status_code=503)
    return await fleet_manager.health_check()


@app.get("/templates")
async def list_templates():
    """List available operation templates."""
    if not fleet_manager:
        raise HTTPException(503, "Fleet manager not initialized")
    
    return {
        tid: {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "trigger": t.trigger,
            "schedule": t.schedule,
            "agents": [a["profile"] for a in t.agents],
            "aggregation": t.aggregation,
            "timeout_seconds": t.timeout_seconds,
        }
        for tid, t in fleet_manager.config.operations.items()
    }


@app.get("/profiles")
async def list_profiles():
    """List available agent profiles."""
    if not fleet_manager:
        raise HTTPException(503, "Fleet manager not initialized")
    
    return {
        name: {
            "name": p.name,
            "title": p.title,
            "description": p.description,
            "model": p.model,
            "skills": p.skills,
            "toolsets": p.toolsets,
        }
        for name, p in fleet_manager.config.profiles.items()
    }


if __name__ == "__main__":
    import uvicorn
    
    # Ensure var directory exists
    Path("/home/ais04/thaqip/agent-fleet/var").mkdir(parents=True, exist_ok=True)
    
    # Write PID file
    pid_file = Path("/home/ais04/thaqip/agent-fleet/var/fleet-daemon.pid")
    pid_file.write_text(str(os.getpid()))
    
    config = FleetConfig("/home/ais04/thaqip/agent-fleet/config/fleet.yaml")
    api_port = config.fleet.get("daemon", {}).get("api_port", 8766)
    
    uvicorn.run(app, host="0.0.0.0", port=api_port, log_level="info")