const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync('src/eutonos/dashboard_static/app.js', 'utf8');
const chartContext = vm.createContext({shortModel: value => value});
vm.runInContext(
  source.slice(source.indexOf('function calendarRangeStart'), source.indexOf('async function loadMetricProjection')),
  chartContext,
);
vm.runInContext(
  source.slice(source.indexOf('function tokenCounter'), source.indexOf('function modelTokenChart')),
  chartContext,
);

const calendarRangeStart = vm.runInContext('calendarRangeStart', chartContext);
const tokenGroups = vm.runInContext('tokenGroups', chartContext);
const modelUsageSlices = vm.runInContext('modelUsageSlices', chartContext);
const visibleOutputTokens = vm.runInContext('visibleOutputTokens', chartContext);

assert.equal(calendarRangeStart('2024-03-31T12:00:00Z', 1, 'months').toISOString(), '2024-02-29T12:00:00.000Z');
assert.equal(calendarRangeStart('2024-02-29T12:00:00Z', 1, 'years').toISOString(), '2023-02-28T12:00:00.000Z');

const modelRows = [
  {model: 'm', tokens: {input_tokens: 100, output_tokens: 20, reasoning_output_tokens: 8}},
  {model: 'm', tokens: {input_tokens: 50, output_tokens: 10, reasoning_output_tokens: 2}},
];
const group = tokenGroups(modelRows, [], false)[0];
assert.equal(group.reasoning, 10);
assert.equal(group.output, 20);
assert.equal(group.fullOutput, 30);
assert.equal(visibleOutputTokens({output_tokens: 10, reasoning_output_tokens: 3}), 7);
assert.equal(visibleOutputTokens({output_tokens: 3, reasoning_output_tokens: 4}), null);
assert.equal(tokenGroups([{model: 'm', tokens: {}}], [], false)[0].input, null);

const pie = modelUsageSlices([...modelRows, {model: 'n', tokens: {observed_input_tokens: 45, observed_output_tokens: 15}}]);
assert.equal(pie[0].label, 'm');
assert.equal(pie[0].value, 180);
assert.equal(pie[0].share, .75);
assert.equal(pie[1].partial, true);

const historyContext = vm.createContext({
  esc: value => String(value),
  num: value => new Intl.NumberFormat('en-US').format(value),
  compactNum: value => String(value),
  exactValue: value => Number.isFinite(value) ? String(value) : 'Unknown',
});
vm.runInContext(
  source.slice(source.indexOf('function historyPoints'), source.indexOf('function moduleSummary')),
  historyContext,
);
const historyPoints = vm.runInContext('historyPoints', historyContext);
const tokenHistory = vm.runInContext('tokenHistory', historyContext);
const trend = [{
  date: '2026-09-15',
  tokens: {input_tokens: 1000, output_tokens: 90, reasoning_output_tokens: 70},
  observed_standard_api_estimate_usd: 1.23456,
}];
const history = historyPoints(trend)[0];
assert.equal(history.input, 1000);
assert.equal(history.thinking, 70);
assert.equal(history.output, 20);
assert.equal(history.total, 1090);
assert.equal(history.cost, 1.23456);

const markup = tokenHistory(trend, 'Recorded history');
assert.match(markup, /Input/);
assert.match(markup, /Thinking/);
assert.match(markup, /Output/);
assert.match(markup, /Total/);
assert.match(markup, /r="5"/);
assert.match(markup, /tabindex="0"/);
assert.match(markup, /2026-09-15\. Input: 1,000 tokens\. Standard API equivalent: \$1\.2346\./);
assert.match(markup, /All four lines share one scale/);

console.log('Calendar boundaries, token splits, model aggregation, pie shares, daily cost tooltips and unknown counts passed.');
