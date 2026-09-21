import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import http from 'node:http';
import test from 'node:test';
import {
  buildUpstreamHeaders,
  createAccessVerifier,
  createGatewayServer,
  isAllowedPath,
  isLoopbackAddress,
  loadConfig,
} from '../../public/gateway/gateway.mjs';

const validEnv = {
  ACCESS_TEAM_DOMAIN: 'example.cloudflareaccess.com',
  ACCESS_AUD: 'aud-123',
  ACCESS_ALLOWED_EMAILS: 'user@example.com',
};

test('gateway fails closed without Cloudflare Access configuration', () => {
  assert.throws(() => loadConfig({}), /ACCESS_TEAM_DOMAIN/);
  assert.throws(() => loadConfig({ ACCESS_TEAM_DOMAIN: 'example.cloudflareaccess.com' }), /ACCESS_AUD/);
  assert.throws(() => loadConfig({
    ACCESS_TEAM_DOMAIN: 'example.cloudflareaccess.com',
    ACCESS_AUD: 'aud-123',
  }), /ACCESS_ALLOWED_EMAILS/);
});

test('gateway defaults to the dedicated Avanza upstream port', () => {
  const config = loadConfig(validEnv);
  assert.equal(config.mode, 'cloudflare');
  assert.equal(config.bindHost, '0.0.0.0');
  assert.equal(config.upstreamPort, 8767);
  assert.equal(config.upstreamPath, '/mcp');
  assert.equal(config.port, 8080);
});

test('local gateway is loopback-only and needs no Cloudflare credentials', () => {
  const config = loadConfig({ GATEWAY_MODE: 'local' });
  assert.equal(config.mode, 'local');
  assert.equal(config.bindHost, '127.0.0.1');
  assert.equal(config.upstreamHost, '127.0.0.1');
  assert.equal(config.upstreamPort, 8767);
  assert.equal(config.port, 8769);
  assert.equal(config.ratePerMin, 600);

  assert.equal(isLoopbackAddress('127.0.0.1'), true);
  assert.equal(isLoopbackAddress('::1'), true);
  assert.equal(isLoopbackAddress('::ffff:127.0.0.1'), true);
  assert.equal(isLoopbackAddress('192.0.2.10'), false);

  assert.throws(
    () => loadConfig({ GATEWAY_MODE: 'local', BIND_HOST: '0.0.0.0' }),
    /BIND_HOST must be loopback/,
  );
  assert.throws(
    () => loadConfig({ GATEWAY_MODE: 'local', UPSTREAM_HOST: '192.0.2.10' }),
    /UPSTREAM_HOST must be loopback/,
  );
});

test('only the MCP path is accepted', () => {
  assert.equal(isAllowedPath('/mcp'), true);
  assert.equal(isAllowedPath('/mcp?session=1'), true);
  assert.equal(isAllowedPath('/'), false);
  assert.equal(isAllowedPath('/healthz'), false);
  assert.equal(isAllowedPath('/mcp/extra'), false);
});

test('client and Cloudflare credentials are never forwarded upstream', () => {
  const config = loadConfig(validEnv);
  const headers = buildUpstreamHeaders({
    authorization: 'Bearer secret',
    cookie: 'session=secret',
    'cf-access-jwt-assertion': 'jwt',
    'cf-authorization-token': 'token',
    'cf-connecting-ip': '203.0.113.5',
    'x-forwarded-for': '203.0.113.5',
    origin: 'https://chatgpt.com',
    referer: 'https://chatgpt.com/',
    'mcp-session-id': 'keep-me',
    accept: 'application/json, text/event-stream',
    'content-length': '999',
  }, config, 12);

  for (const name of [
    'authorization', 'cookie', 'cf-access-jwt-assertion', 'cf-authorization-token',
    'cf-connecting-ip', 'x-forwarded-for', 'origin', 'referer',
  ]) {
    assert.equal(name in headers, false, name);
  }
  assert.equal(headers.host, 'localhost:8767');
  assert.equal(headers['mcp-session-id'], 'keep-me');
  assert.equal(headers.accept, 'application/json, text/event-stream');
  assert.equal(headers['content-length'], '12');
});

function makeJwt(privateKey, kid, claims, alg = 'RS256') {
  const header = Buffer.from(JSON.stringify({ alg, typ: 'JWT', kid })).toString('base64url');
  const payload = Buffer.from(JSON.stringify(claims)).toString('base64url');
  const data = `${header}.${payload}`;
  const signature = crypto.sign('RSA-SHA256', Buffer.from(data), privateKey).toString('base64url');
  return `${data}.${signature}`;
}

test('Access verifier accepts only a valid signature, audience, issuer and allowed email', async () => {
  const { privateKey, publicKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
  const kid = 'test-key';
  const jwk = publicKey.export({ format: 'jwk' });
  jwk.kid = kid;
  jwk.alg = 'RS256';
  jwk.use = 'sig';

  let fetches = 0;
  const fetchImpl = async () => {
    fetches += 1;
    return { ok: true, json: async () => ({ keys: [jwk] }) };
  };
  const config = loadConfig(validEnv);
  const verify = createAccessVerifier(config, fetchImpl);
  const now = Math.floor(Date.now() / 1000);
  const base = {
    aud: ['aud-123'],
    iss: 'https://example.cloudflareaccess.com',
    email: 'USER@example.com',
    exp: now + 300,
    nbf: now - 5,
  };

  assert.deepEqual(await verify(makeJwt(privateKey, kid, base)), { ok: true, email: 'user@example.com' });
  assert.equal(fetches, 1);

  const wrongAudience = await verify(makeJwt(privateKey, kid, { ...base, aud: ['other'] }));
  assert.equal(wrongAudience.ok, false);
  assert.equal(wrongAudience.reason, 'wrong audience');

  const wrongEmail = await verify(makeJwt(privateKey, kid, { ...base, email: 'other@example.com' }));
  assert.equal(wrongEmail.ok, false);
  assert.match(wrongEmail.reason, /email not allowed/);

  const missing = await verify(null);
  assert.deepEqual(missing, { ok: false, reason: 'missing token' });
});

test('authenticated MCP traffic is proxied, compacted, and blocked tools never reach upstream', async t => {
  const { privateKey, publicKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
  const kid = 'e2e-key';
  const jwk = publicKey.export({ format: 'jwk' });
  Object.assign(jwk, { kid, alg: 'RS256', use: 'sig' });

  let upstreamCalls = 0;
  let upstreamHeaders = null;
  const upstream = http.createServer(async (req, res) => {
    upstreamCalls += 1;
    upstreamHeaders = req.headers;
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const request = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    const response = request.method === 'tools/list'
      ? {
          jsonrpc: '2.0', id: request.id, result: { tools: [
            {
              name: 'search_instruments',
              description: 'Search instruments. '.repeat(30),
              inputSchema: { type: 'object', properties: { query: { type: 'string' } }, required: ['query'] },
              outputSchema: { type: 'object', properties: { data: { type: 'array' } } },
            },
            { name: 'future_place_order', description: 'Must stay private', inputSchema: { type: 'object' } },
          ] },
        }
      : { jsonrpc: '2.0', id: request.id, result: {} };
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify(response));
  });
  await new Promise(resolve => upstream.listen(0, '127.0.0.1', resolve));
  t.after(() => upstream.close());
  const upstreamPort = upstream.address().port;

  const env = {
    ...validEnv,
    UPSTREAM_HOST: '127.0.0.1',
    UPSTREAM_PORT: String(upstreamPort),
    UPSTREAM_HOST_HEADER: 'localhost',
  };
  const fetchImpl = async () => ({ ok: true, json: async () => ({ keys: [jwk] }) });
  const gateway = createGatewayServer(env, { fetch: fetchImpl });
  await new Promise(resolve => gateway.listen(0, '127.0.0.1', resolve));
  t.after(() => gateway.close());
  const gatewayPort = gateway.address().port;

  const now = Math.floor(Date.now() / 1000);
  const token = makeJwt(privateKey, kid, {
    aud: ['aud-123'],
    iss: 'https://example.cloudflareaccess.com',
    email: 'user@example.com',
    exp: now + 300,
  });

  const listed = await fetch(`http://127.0.0.1:${gatewayPort}/mcp`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'cf-access-jwt-assertion': token,
      authorization: 'Bearer client-secret-must-not-forward',
    },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list', params: {} }),
  });
  assert.equal(listed.status, 200);
  const listedBody = await listed.json();
  assert.equal(listedBody.result.tools.length, 1);
  assert.equal(listedBody.result.tools[0].name, 'search_instruments');
  assert.equal('outputSchema' in listedBody.result.tools[0], false);
  assert.equal(upstreamCalls, 1);
  assert.equal('authorization' in upstreamHeaders, false);
  assert.equal('cf-access-jwt-assertion' in upstreamHeaders, false);

  const blocked = await fetch(`http://127.0.0.1:${gatewayPort}/mcp`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'cf-access-jwt-assertion': token },
    body: JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'future_place_order', arguments: {} } }),
  });
  const blockedBody = await blocked.json();
  assert.equal(blocked.status, 200);
  assert.equal(blockedBody.error.code, -32601);
  assert.equal(upstreamCalls, 1);
});


test('local gateway proxies without JWT and compacts model-facing results', async t => {
  let upstreamCalls = 0;
  const upstream = http.createServer(async (req, res) => {
    upstreamCalls += 1;
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const request = JSON.parse(Buffer.concat(chunks).toString('utf8'));

    let response;
    if (request.method === 'tools/list') {
      response = {
        jsonrpc: '2.0',
        id: request.id,
        result: {
          tools: [{
            name: 'get_stock_quote',
            description: 'Quote description. '.repeat(30),
            inputSchema: {
              type: 'object',
              properties: { order_book_id: { type: 'string', description: 'Identifier'.repeat(20) } },
              required: ['order_book_id'],
            },
            outputSchema: { type: 'object', properties: { last: { type: 'number' } } },
          }],
        },
      };
    } else {
      response = {
        jsonrpc: '2.0',
        id: request.id,
        result: {
          content: [{ type: 'text', text: '{"last":123.45}' }],
          structuredContent: { last: 123.45 },
          isError: false,
        },
      };
    }

    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify(response));
  });
  await new Promise(resolve => upstream.listen(0, '127.0.0.1', resolve));
  t.after(() => upstream.close());

  const gateway = createGatewayServer({
    GATEWAY_MODE: 'local',
    UPSTREAM_HOST: '127.0.0.1',
    UPSTREAM_PORT: String(upstream.address().port),
    ALLOWED_TOOLS: 'get_stock_quote',
  });
  await new Promise(resolve => gateway.listen(0, '127.0.0.1', resolve));
  t.after(() => gateway.close());
  const gatewayPort = gateway.address().port;

  const listed = await fetch(`http://127.0.0.1:${gatewayPort}/mcp`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list', params: {} }),
  });
  assert.equal(listed.status, 200);
  const listedBody = await listed.json();
  assert.equal(listedBody.result.tools.length, 1);
  assert.equal('outputSchema' in listedBody.result.tools[0], false);
  assert.ok(listedBody.result.tools[0].description.length <= 180);

  const called = await fetch(`http://127.0.0.1:${gatewayPort}/mcp`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 2,
      method: 'tools/call',
      params: { name: 'get_stock_quote', arguments: { order_book_id: '4478' } },
    }),
  });
  assert.equal(called.status, 200);
  const calledBody = await called.json();
  assert.deepEqual(calledBody.result.content, [{ type: 'text', text: '{"last":123.45}' }]);
  assert.equal('structuredContent' in calledBody.result, false);
  assert.equal(upstreamCalls, 2);
});
