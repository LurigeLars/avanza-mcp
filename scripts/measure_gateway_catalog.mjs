import fs from 'node:fs';
import { compactToolDefinition } from '../public/gateway/policy.mjs';

const input = fs.readFileSync(0, 'utf8');
const tools = JSON.parse(input);
const compacted = tools.map(compactToolDefinition);
const bytes = value => Buffer.byteLength(JSON.stringify(value));

function stripSchemaDescriptions(value) {
  if (Array.isArray(value)) return value.map(stripSchemaDescriptions);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => key !== 'description')
      .map(([key, item]) => [key, stripSchemaDescriptions(item)]),
  );
}

const descriptionless = compacted.map(tool => ({
  ...tool,
  inputSchema: stripSchemaDescriptions(tool.inputSchema),
}));

const toolSizes = compacted
  .map(tool => ({ name: tool.name, bytes: bytes(tool) }))
  .sort((a, b) => b.bytes - a.bytes)
  .slice(0, 10);

process.stdout.write(JSON.stringify({
  tool_count: tools.length,
  raw_catalog_bytes: bytes({ tools }),
  compacted_catalog_bytes: bytes({ tools: compacted }),
  compacted_without_schema_descriptions_bytes: bytes({ tools: descriptionless }),
  schema_description_bytes: bytes({ tools: compacted }) - bytes({ tools: descriptionless }),
  saved_bytes: bytes({ tools }) - bytes({ tools: compacted }),
  saved_percent: Number((((bytes({ tools }) - bytes({ tools: compacted })) / bytes({ tools })) * 100).toFixed(2)),
  raw_tools_with_output_schema: tools.filter(tool => tool.outputSchema).length,
  compacted_tools_with_output_schema: compacted.filter(tool => tool.outputSchema).length,
  largest_compacted_tools: toolSizes,
}));
