import { expect, it } from 'vitest';
import { AxiosError } from 'axios';
import { apiError } from '../src/api';
it('distinguishes a timed-out request from an unreachable server', () => {
  expect(apiError(new AxiosError('timeout', 'ECONNABORTED'))).toContain('Request timed out');
  expect(apiError(new AxiosError('network', 'ERR_NETWORK'))).toContain('Cannot reach API');
});
it('does not render validation arrays as objects', () => {
  const error = new AxiosError('validation');
  error.response = { data: { detail: [{ msg: 'invalid', loc: ['body'] }] }, status: 422, statusText: 'Invalid', headers: {}, config: {} as never };
  expect(apiError(error)).toContain('Invalid request');
  expect(apiError(error)).not.toContain('[object Object]');
});
