import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildUpstreamHeaders,
  loadConfig,
} from '../../public/gateway/gateway.mjs';

const validEnv = {
  ACCESS_TEAM_DOMAIN: 'example.cloudflareaccess.com',
  ACCESS_AUD: 'aud-123',
  ACCESS_ALLOWED_EMAILS: 'user@example.com',
};

test('gateway strips attacker-supplied x-securitytoken before upstream forwarding', () => {
  const config = loadConfig(validEnv);
  const headers = buildUpstreamHeaders({
    'x-securitytoken': 'attacker-controlled-value',
    'mcp-session-id': 'keep-me',
    accept: 'application/json',
  }, config, 0);

  assert.equal('x-securitytoken' in headers, false);
  assert.equal(headers['mcp-session-id'], 'keep-me');
  assert.equal(headers.accept, 'application/json');
});
