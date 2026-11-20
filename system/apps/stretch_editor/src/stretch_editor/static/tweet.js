// Tweet preview: draws a version as an X post with xeet (X's own layout, fonts and line breaking,
// compiled to WebAssembly), loaded from this app's origin the first time a preview is opened.
let xeetReady = null;
let attempt = 0;

async function loadXeet() {
  xeetReady ??= (async () => {
    // A failed module load is remembered by the browser for that address, so each retry uses a fresh one.
    const wasm = await import(`/vendor/xeet/wasm/xeet_wasm.esm.js?try=${attempt++}`);
    await wasm.default('/vendor/xeet/wasm/xeet_wasm_bg.wasm');
    const { bind } = await import('/vendor/xeet/src/abi.js');
    const { createFontPacks, sha256Hex } = await import('/vendor/xeet/src/fonts.js');
    const packs = createFontPacks({
      hash: sha256Hex,
      register: (file, bytes) => wasm.register_font(file, bytes),
      missingPacks: (request) => wasm.missing_packs(request),
      manifest: await fetch('/vendor/xeet/wasm/fonts/packs.json').then((r) => r.json()),
      read: (name) =>
        fetch(`/vendor/xeet/wasm/fonts/${name}.bin`)
          .then((r) => r.arrayBuffer())
          .then((b) => new Uint8Array(b))
    });
    return { wasm, xeet: bind(wasm), packs };
  })();
  try {
    return await xeetReady;
  } catch (e) {
    xeetReady = null;
    throw e;
  }
}

// Returns an object URL for a PNG of the post.
export async function renderTweet({ text, name, handle, verified = 'none', avatar = null, theme = 'light' }) {
  const { wasm, xeet, packs } = await loadXeet();
  const now = new Date();
  const post = {
    text,
    author: { name, handle, verified, ...(avatar ? { avatar } : {}) },
    createdAt: new Date(now.getTime() - 2 * 3600 * 1000).toISOString(),
    now: now.toISOString(),
    counts: { replies: 18, reposts: 42, likes: 316, views: 12800 }
  };
  const options = { surface: 'web-timeline', width: 560, theme, dpr: 2 };
  await packs.ensure({ op: 'render', post, options });
  const { png } = xeet.render(post, options);
  return URL.createObjectURL(new Blob([png], { type: 'image/png' }));
}
