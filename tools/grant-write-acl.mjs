const base = `${process.env.APPDATA?.replace(/\\/g, "/")}/npm/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/`;
const { AclWriteGrant, workspaceWriteSid } = await import(base + 'dsh-sandbox-windows-acl/lib/index.js');

const sid = workspaceWriteSid('E:\\ai-talk');
console.log('workspace write SID =', sid);
const grant = AclWriteGrant.create(sid);
try {
  for (const p of ['D:\\dsh-memory-system']) {
    grant.add(p, true);
    console.log('granted', p);
  }
  console.log('done');
} catch (e) {
  console.error('failed:', e && e.message ? e.message : e);
  process.exitCode = 1;
}
