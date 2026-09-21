import fs from 'node:fs';
import { isDeepStrictEqual } from 'node:util';

export const DEFAULT_ALLOWED_TOOLS = [
  'search_instruments',
  'get_instrument_by_order_book_id',
  'get_stock_info',
  'get_stock_quote',
  'get_stock_chart',
  'get_stock_analysis',
  'get_dividends',
  'get_company_financials',
  'get_orderbook',
  'get_marketplace_info',
  'get_recent_trades',
  'get_broker_trade_summary',
  'get_fund_info',
  'get_fund_sustainability',
  'get_fund_chart',
  'get_fund_chart_periods',
  'get_fund_description',
  'get_fund_holdings',
  'filter_certificates',
  'get_certificate_info',
  'get_certificate_details',
  'filter_warrants',
  'get_warrant_info',
  'get_warrant_details',
  'filter_etfs',
  'get_etf_info',
  'get_etf_details',
  'list_futures_forwards',
  'get_future_forward_filter_options',
  'get_future_forward_info',
  'get_future_forward_details',
  'get_number_of_owners',
  'get_short_selling',
  'get_marketmaker_chart',
  'screen_leveraged_instruments',
].join(',');

export function parseAllowedTools(value) {
  return new Set((value || DEFAULT_ALLOWED_TOOLS).split(',').map(item => item.trim()).filter(Boolean));
}

export function loadInstructions() {
  try {
    return fs.readFileSync(new URL('./instructions.md', import.meta.url), 'utf8').trim() || null;
  } catch {
    return null;
  }
}

function compactText(value, maxChars) {
  const text = String(value ?? '').replace(/\s+/g, ' ').trim();
  if (text.length <= maxChars) return text;
  const firstSentence = text.match(/^.*?[.!?](?:\s|$)/)?.[0]?.trim();
  if (firstSentence && firstSentence.length <= maxChars) return firstSentence;
  return text.slice(0, Math.max(1, maxChars - 1)).trimEnd() + '…';
}

export function compactSchema(value) {
  if (Array.isArray(value)) return value.map(compactSchema);
  if (!value || typeof value !== 'object') return value;

  const out = {};
  for (const [key, item] of Object.entries(value)) {
    if (key === '$schema' || key === 'title' || key === 'examples') continue;
    if (key === 'description') {
      out[key] = compactText(item, 96);
      continue;
    }
    out[key] = compactSchema(item);
  }
  return out;
}

export function compactToolDefinition(tool) {
  if (!tool || typeof tool !== 'object') return tool;
  const out = { ...tool };
  if (out.description) out.description = compactText(out.description, 180);
  if (out.inputSchema) out.inputSchema = compactSchema(out.inputSchema);

  // ChatGPT only needs the input contract before invocation. The upstream server
  // still validates and returns its structured result; omitting outputSchema here
  // avoids repeating large result schemas in model context on every connection.
  delete out.outputSchema;
  return out;
}

function hasEquivalentTextAndStructuredResult(result) {
  if (!Array.isArray(result?.content) || result.content.length !== 1) return false;
  if (result?.structuredContent === undefined) return false;
  const item = result.content[0];
  if (item?.type !== 'text' || typeof item.text !== 'string') return false;

  try {
    return isDeepStrictEqual(JSON.parse(item.text), result.structuredContent);
  } catch {
    return false;
  }
}

export function rewriteResponse(message, { allowedTools, compactToolResults = false }) {
  if (!message || typeof message !== 'object') return message;

  if (compactToolResults && hasEquivalentTextAndStructuredResult(message.result)) {
    // FastMCP typed tools commonly serialize the same JSON-compatible result twice.
    // Drop structuredContent only after proving the text representation is equivalent.
    delete message.result.structuredContent;
  }

  if (message.result?.tools) {
    message.result.tools = message.result.tools
      .filter(tool => allowedTools.has(tool.name))
      .map(compactToolDefinition);
  }

  if (message.result?.serverInfo) {
    const instructions = loadInstructions();
    if (instructions) message.result.instructions = instructions;
  }

  return message;
}

export function checkRequest(message, allowedTools) {
  if (message?.method !== 'tools/call') return {};
  if (!allowedTools.has(message.params?.name)) {
    return { error: `Tool not available on the public Avanza connector: ${message.params?.name}` };
  }
  return {};
}

export const rpcError = (id, message) => ({
  jsonrpc: '2.0',
  id: id ?? null,
  error: { code: -32601, message },
});
