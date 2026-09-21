import fs from 'node:fs';
import { compactToolDefinition } from '../public/gateway/policy.mjs';

const input = fs.readFileSync(0, 'utf8');
const tools = JSON.parse(input);
const compacted = tools.map(compactToolDefinition);
const bytes = value => Buffer.byteLength(JSON.stringify(value));

process.stdout.write(JSON.stringify({
  tool_count: tools.length,
  raw_catalog_bytes: bytes({ tools }),
  compacted_catalog_bytes: bytes({ tools: compacted }),
  saved_bytes: bytes({ tools }) - bytes({ tools: compacted }),
  saved_percent: Number((((bytes({ tools }) - bytes({ tools: compacted })) / bytes({ tools })) * 100).toFixed(2)),
  raw_tools_with_output_schema: tools.filter(tool => tool.outputSchema).length,
  compacted_tools_with_output_schema: compacted.filter(tool => tool.outputSchema).length,
}));
