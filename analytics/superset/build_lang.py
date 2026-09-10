"""Compile Superset's Arabic catalog for the lean image, which ships only .po.

Backend strings need messages.mo (pybabel compile); the React front end
fetches /superset/language_pack/<lang>/ which serves messages.json in Jed 1.x
format. Untranslated entries are left out so they fall back to English
instead of rendering blank, and so are entries that fail Babel's checks
(upstream's Arabic catalog has a few with mismatched format placeholders,
which would make pybabel refuse the whole file and could break formatting at
runtime). Both files are written here, so pybabel compile is not needed.
"""
import json
import sys
from pathlib import Path

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

CONTEXT_SEPARATOR = "\x04"   # Jed joins msgctxt and msgid with EOT

lang = sys.argv[1] if len(sys.argv) > 1 else "ar"
base = Path("/app/superset/translations") / lang / "LC_MESSAGES"
with open(base / "messages.po", "rb") as fh:
    catalog = read_po(fh, locale=lang)

# Curated corrections for the most visible strings. Upstream's Arabic has
# some literal mistranslations ("Save" as "save money", "Run" as "jog") and
# a few garbled ones; these are applied to both the .mo and the JSON.
overrides_path = Path(__file__).with_name(f"{lang}_overrides.json")
overrides = json.loads(overrides_path.read_text()) if overrides_path.exists() else {}
for msgid, text in overrides.items():
    msg = catalog.get(msgid)
    if msg is None:
        catalog.add(msgid, text)
    elif not isinstance(msg.id, (list, tuple)):
        msg.string = text
        msg.flags.discard("fuzzy")

broken = 0
for msg, errors in list(catalog.check()):
    if errors:
        msg.string = ("", "") if isinstance(msg.id, (list, tuple)) else ""
        broken += 1

with open(base / "messages.mo", "wb") as fh:
    write_mo(fh, catalog)

entries = {"": {"domain": "superset", "lang": lang,
                "plural_forms": catalog.plural_forms}}
kept = 0
for msg in catalog:
    if not msg.id or msg.fuzzy:
        continue
    if isinstance(msg.id, (list, tuple)):          # plural entry
        key = msg.id[0]
        values = list(msg.string)
        if not any(values):
            continue
    else:
        key = msg.id
        if not msg.string:
            continue
        values = [msg.string]
    if msg.context:
        key = msg.context + CONTEXT_SEPARATOR + key
    entries[key] = values
    kept += 1

out = {"domain": "superset", "locale_data": {"superset": entries}}
(base / "messages.json").write_text(json.dumps(out, ensure_ascii=False))
print(f"{lang}: {kept} translated strings -> messages.json + messages.mo "
      f"({broken} dropped for failing placeholder checks, {len(overrides)} curated overrides)")
