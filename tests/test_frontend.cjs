const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function element() {
  return {
    value: '', checked: false, textContent: '', innerHTML: '', style: {},
    classList: { add() {}, remove() {} },
    addEventListener() {}, append() {}, scrollIntoView() {}
  };
}
const context = vm.createContext({
  URL, console, setTimeout,
  document: {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    },
    createElement: element
  }
});
const script = fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'), 'utf8');
vm.runInContext(script, context);
const run = code => vm.runInContext(code, context);
(async () => {
  let resolve;
  context.fetch = () => new Promise(r => { resolve = r; });
  run("input.value = 'https://x.com/author/status/123'");
  const pending = run('prepareTranslations()');
  run("input.value = 'https://x.com/author/status/456'; resetTranslationReview()");
  resolve({ ok: true, json: async () => ({ ok: true, tweetId: '123', items: [] }) });
  await pending;
  assert.equal(run('translationPrepared'), false);
  assert.equal(run('translationTweetId'), null);

  context.fetch = async () => ({
    ok: true, json: async () => ({ ok: true, tweetId: '456', items: [] })
  });
  await run('prepareTranslations()');
  assert.equal(run('translationPrepared'), true);
  assert.equal(run('translationTweetId'), '456');

  let sent;
  context.fetch = async (_url, options) => {
    sent = JSON.parse(options.body);
    return { ok: false, json: async () => ({ error: 'test ends before capture' }) };
  };
  run("input.value = 'https://x.com/author/status/789'");
  await run('capture()');
  assert.equal(sent.translateBody, false);
  assert.equal(sent.translationOverrides, undefined);

  run("updateResult({previewUrl:'/screenshots/test.png', fileName:'test.png', savedTo:'test.png', captureMode:'public_api_fallback', usedUrl:'public-api://fx/status/123', tweetId:'123'})");
  assert.equal(elements.get('captureMode').textContent, '公开数据重绘（非原网页截图）');
  assert.equal(elements.get('usedUrl').href, 'https://x.com/i/status/123');
  console.log('Frontend regression checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
