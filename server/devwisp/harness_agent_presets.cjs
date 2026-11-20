// The SDK profile has no preset binding of its own. The serial native lifecycle
// hook runs before queued input is released and before the first model request.
// Keep the implementation in Harness: scoped tools, skills and session records
// are supplied by its registry, not by role instructions added to user prompts.
exports.name = 'devwisp-agent-presets';
exports.inject = ['agents', 'agentPresets'];
exports.apply = function (ctx, config) {
  ctx.on('agent/created', async ({ agent }) => {
    const presets = ctx.agentPresets;
    // Native descendants already inherit their parent's exact composition.
    if (presets.composedPreset(agent.ctx)) return;
    const recorded = presets.presetForSession(agent.session);
    if (recorded && recorded !== config.preset) {
      throw new Error('Dev Wisp Agent preset does not match the recorded session');
    }
    await presets.mount(agent.ctx, recorded || config.preset);
    if (!recorded) agent.session.append('agent-preset/selected', { agentPreset: config.preset });
  });
};
