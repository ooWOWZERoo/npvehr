// Empty stub for the bare "onnxruntime-web/webgpu" import inside the
// vendored transformers.web.min.js bundle. That import is dead code in this
// build (its namespace, `CS`, is bound but never referenced anywhere in the
// bundle -- confirmed by grepping for any use of it) -- it only exists
// because the bundle was built assuming a bundler would resolve bare
// module specifiers via node_modules. Loaded directly via the browser's
// native ES module loader (this app has no build step), a bare specifier
// with no matching import map entry fails module resolution immediately,
// which silently broke every voice-scribe transcription. An import map
// (see the `{% block extra_js %}` in exams/form.html, exams/detail.html,
// and prescriptions/detail.html) points the bare specifier here instead of
// pulling in the real onnxruntime-web WebGPU package this app never uses
// (voice dictation runs on the WASM backend only).
export {};
