# Vendored library: onnxruntime-web (webgpu entry, MIT-licensed)

`ort.webgpu.bundle.min.1.30.0.mjs` is the official prebuilt browser bundle
from npm's `onnxruntime-web@1.30.0` package (`dist/ort.webgpu.bundle.min.mjs`,
the file its own `package.json` `"exports"."./webgpu".import.default` maps
to), fetched directly from `registry.npmjs.org` and committed here rather
than loaded from a CDN at runtime -- same rationale as the other vendored
libraries in this directory tree (no external runtime dependency for a
clinical app).

This satisfies the vendored `transformers.web.min.js` bundle's bare
`import*as CS from"onnxruntime-web/webgpu"` specifier, remapped via the
`<script type="importmap">` in `exams/form.html`, `exams/detail.html`, and
`prescriptions/detail.html`.

**Why this file, and not the earlier empty stub it replaces**: an earlier
fix (PR #39) stubbed this import out as empty, on the mistaken conclusion
that it was dead code in the WASM-only codepath this app uses. It is not --
in every non-Node (i.e. every real browser) environment, `transformers.js`
unconditionally assigns `Ks = CS` (this import's namespace) as its *sole*
source for `InferenceSession`/`Tensor`/etc, for the WASM backend too, not
just WebGPU. An empty stub left those all `undefined`, throwing
`TypeError: Cannot read properties of undefined (reading 'create')` the
moment a real transcription pipeline tried to create an ONNX session --
this only surfaced once the vendored model weights had actually finished
downloading over a real network, which this sandbox's own network policy
(huggingface.co blocked) can't exercise, so it wasn't caught until a real
end-user browser test.

**Note on the WASM binary**: this bundle resolves its own `.wasm` binary
relative to its own module URL by default, but `voice_scribe.js`
explicitly overrides `env.backends.onnx.wasm.wasmPaths` to point at the
matching `onnxruntime-web@1.30.0` build on the jsdelivr CDN instead --
that binary is ~14-28MB depending on variant, the same class of large,
generic (non-patient) asset as the Whisper model weights themselves, so it
is fetched and cached per-browser rather than committed to this repo.

**Upgrading**: fetch the new version's tarball from
`https://registry.npmjs.org/onnxruntime-web/-/onnxruntime-web-<version>.tgz`,
extract `package/dist/ort.webgpu.bundle.min.mjs`, strip its
`//# sourceMappingURL=` comment, rename to match the new version, update the
import map `<script type="importmap">` src path in all three templates
above and the CDN version pin in `voice_scribe.js`'s `wasmPaths` override,
then delete the old file.
