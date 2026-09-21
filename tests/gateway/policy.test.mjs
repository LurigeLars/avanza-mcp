import assert from 'node:assert/strict';
import test from 'node:test';
import {
  DEFAULT_ALLOWED_TOOLS,
  checkRequest,
  compactSchema,
  compactToolDefinition,
  parseAllowedTools,
  rewriteResponse,
} from '../../public/gateway/policy.mjs';

test('default public allowlist contains the current 35 read-only Avanza tools', () => {
  const allowed = parseAllowedTools();
  assert.equal(allowed.size, 35);
  assert.equal(allowed.has('search_instruments'), true);
  assert.equal(allowed.has('get_orderbook'), true);
  assert.equal(allowed.has('get_marketmaker_chart'), true);
  assert.equal(DEFAULT_ALLOWED_TOOLS.split(',').length, 35);
});

test('explicit ALLOWED_TOOLS override is authoritative', () => {
  const allowed = parseAllowedTools('search_instruments, get_stock_quote');
  assert.deepEqual([...allowed], ['search_instruments', 'get_stock_quote']);
});

test('non-allowlisted tool calls are blocked', () => {
  const allowed = parseAllowedTools('search_instruments');
  assert.deepEqual(checkRequest({
    method: 'tools/call',
    params: { name: 'future_place_order' },
  }, allowed), {
    error: 'Tool not available on the public Avanza connector: future_place_order',
  });
});

test('schema compaction preserves contract-bearing keys and trims prose', () => {
  const schema = compactSchema({
    $schema: 'https://json-schema.org/draft/2020-12/schema',
    title: 'Example',
    description: 'x'.repeat(300),
    type: 'object',
    properties: {
      side: {
        title: 'Side',
        description: 'A sufficiently long description '.repeat(10),
        type: 'string',
        enum: ['buy', 'sell'],
        default: 'buy',
        examples: ['buy'],
      },
    },
    required: ['side'],
  });

  assert.equal('$schema' in schema, false);
  assert.equal('title' in schema, false);
  assert.equal(schema.type, 'object');
  assert.deepEqual(schema.required, ['side']);
  assert.deepEqual(schema.properties.side.enum, ['buy', 'sell']);
  assert.equal(schema.properties.side.default, 'buy');
  assert.equal('examples' in schema.properties.side, false);
  assert.ok(schema.description.length <= 96);
  assert.ok(schema.properties.side.description.length <= 96);
});

test('tool compaction strips outputSchema but preserves annotations and input shape', () => {
  const tool = compactToolDefinition({
    name: 'get_stock_quote',
    description: 'Long description '.repeat(30),
    annotations: { readOnlyHint: true },
    inputSchema: {
      type: 'object',
      properties: { order_book_id: { type: 'string', description: 'Order book identifier' } },
      required: ['order_book_id'],
    },
    outputSchema: { type: 'object', properties: { last: { type: 'number' } } },
  });

  assert.equal(tool.name, 'get_stock_quote');
  assert.deepEqual(tool.annotations, { readOnlyHint: true });
  assert.deepEqual(tool.inputSchema.required, ['order_book_id']);
  assert.equal('outputSchema' in tool, false);
  assert.ok(tool.description.length <= 180);
});

test('tools/list is filtered and compacted', () => {
  const message = {
    result: {
      tools: [
        { name: 'search_instruments', description: 'A'.repeat(300), inputSchema: { type: 'object' }, outputSchema: { type: 'object' } },
        { name: 'future_place_order', description: 'B', inputSchema: { type: 'object' } },
      ],
    },
  };
  const rewritten = rewriteResponse(message, { allowedTools: parseAllowedTools('search_instruments') });
  assert.equal(rewritten.result.tools.length, 1);
  assert.equal(rewritten.result.tools[0].name, 'search_instruments');
  assert.equal('outputSchema' in rewritten.result.tools[0], false);
});
