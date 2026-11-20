// The SDK profile forwards durable messages but does not forward live token
// frames. Emit complete JSON-RPC notification lines on its reserved stdio pipe.
// No plugin dependencies or changes to Harness's persisted session log.
exports.name = 'devwisp-assistant-stream';
exports.inject = ['agents'];
exports.apply = function (ctx, config = {}) {
  // SDK 0.2.2rc2's shipped flock addon requires AVX. Some QEMU CPU profiles
  // lack AVX; use the same official C source built for baseline x86-64.
  // This changes no lock semantics or sandbox launcher.
  if (config.flockAddonPath) {
    const original = process.dlopen;
    const replacement = config.flockAddonPath;
    const wrapped = function (module, filename, ...rest) {
      const selected = typeof filename === 'string' && /(?:^|[\\/])system\.node$/.test(filename) ? replacement : filename;
      return original.call(this, module, selected, ...rest);
    };
    process.dlopen = wrapped;
    ctx.effect(() => () => { if (process.dlopen === wrapped) process.dlopen = original; });
  }
  ctx.on('agent/assistant-stream', ({ agent, frame }) => {
    process.stdout.write(JSON.stringify({
      jsonrpc: '2.0', method: 'session.stream',
      params: { sessionId: String(agent.session.id), frame },
    }) + '\n');
  });
};
