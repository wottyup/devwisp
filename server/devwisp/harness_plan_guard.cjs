// Plan instructions are guidance; this global native guard is the execution
// boundary, including descendant agents and PTC's nested tool pipeline.
exports.name = 'devwisp-plan-guard';
exports.inject = ['agents', 'tools', 'permissionPresets', 'systemPrompt'];
exports.apply = function (ctx, config) {
  if (typeof ctx.tools.guard !== 'function' || !config.sessionId) {
    throw new Error('Dev Wisp plan mode requires native execution guards');
  }
  const inspection = new Set(['read', 'read_image', 'glob', 'grep', 'ask_user_question', 'exit_plan_mode']);
  let root;
  let active = true; // Fail closed until this runtime observes native plan state.
  let reviewed = false;
  const executing = () => reviewed && active === false;

  // PTC exposes only run_code. Before approval accept one literal call into
  // the inspection SDK, never general JavaScript, imports, shell or network.
  // JSON.parse validates the entire argument; no evaluated-code classifier.
  function inspectionProgram(code) {
    if (typeof code !== 'string') return false;
    const match = /^\s*await tools\.(\w+)\(([\s\S]*)\)\s*;?\s*$/.exec(code)
      || /^\s*console\.log\(\s*await tools\.(\w+)\(([\s\S]*)\)\s*\)\s*;?\s*$/.exec(code);
    if (!match || !inspection.has(match[1])) return false;
    try {
      const args = JSON.parse(match[2]);
      return args !== null && typeof args === 'object' && !Array.isArray(args)
        && !Object.hasOwn(args, 'sandbox_permissions');
    } catch { return false; }
  }
  function denial(exec) {
    if (root) ctx.permissionPresets.set(root.session, executing() ? 'workspace-write' : 'read-only');
    if (executing()) return;
    const args = exec.arguments;
    if (args && Object.hasOwn(args, 'sandbox_permissions')) {
      return 'Plan mode: command escalation cannot approve a plan. Submit exit_plan_mode for explicit review first.';
    }
    if (inspection.has(exec.name) && (exec.name !== 'exit_plan_mode' || exec.agent === root)) return;
    if (exec.name === 'run_code' && inspectionProgram(args?.code)) return;
    return 'Plan mode: execution is blocked until the user approves exit_plan_mode. Only read, read_image, glob, grep, ask_user_question and exit_plan_mode are allowed. In PTC use a single await tools.NAME(JSON_OBJECT) call; no general program, shell, MCP, delegation or plugin changes.';
  }
  // Reject before other approval handlers, then enforce again with the native
  // monotonic guard, which cannot be overridden by a later allow decision.
  ctx.on('tools/pre-execute', async (exec, next) => {
    const reason = denial(exec);
    return reason ? { kind: 'deny', reason } : next();
  });
  ctx.tools.guard(denial);
  ctx.on('agent/created', ({ agent }) => {
    if (String(agent.session.id) === config.sessionId) root = agent;
  });
  // planMode is isolated inside each preset; observe its durable session event
  // instead of trying to resolve that private service from the host context.
  ctx.on('session/event', (session, event) => {
    if (String(session.id) !== config.sessionId) return;
    if (event.type === 'turn/end') reviewed = false;
    if (event.type !== 'plan/mode') return;
    active = event.data.active !== false;
    if (active) reviewed = false;
  });
  ctx.on('tools/result', (exec, result) => {
    if (exec.agent === root && exec.name === 'exit_plan_mode'
        && result.isError === false && result.value?.approved === true) {
      reviewed = true;
      ctx.permissionPresets.set(root.session, 'workspace-write');
    }
  });
  // Native plan/mode becomes inactive at the *next* step, so reviewed alone
  // cannot unlock additional calls in the review batch. Reading that committed
  // event also avoids pre-step listener-order differences in Legacy SDK chats.
  ctx.systemPrompt.section({
    name: 'devwisp:plan-execution-policy', order: 999,
    text: () => executing() ? '' : 'Dev Wisp enforces plan approval in tool execution. Until exit_plan_mode is explicitly approved, use only read, read_image, glob, grep, ask_user_question and exit_plan_mode. Commands, writes, MCP, delegation and plugin changes are blocked. In PTC, run_code may contain only a single await tools.NAME(JSON_OBJECT) call to these tools (optionally wrapped in console.log); no other program code. Approval permits implementation from the next step within workspace-write permissions.',
  });
};
