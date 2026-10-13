"use strict";
const $ = (id) => document.getElementById(id);
const escapeHTML = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const names = {planning:"规划中",running:"执行中",verifying:"验证中",cancelling:"停止中",ready:"待审阅",applied:"已合并",conflict:"存在冲突",failed:"执行失败",cancelled:"已取消",interrupted:"已中断",pending:"等待依赖",completed:"已完成",blocked:"依赖受阻",idle:"等待任务"};
const activeStatuses = new Set(["planning","running","verifying","cancelling"]);
let state = null, run = null, runId = null, page = "workspace", statePolling = false, runPolling = false, toastTimer = null, lastRunJSON = "";

async function api(path, data) {
  const response = await fetch(path, data === undefined ? {cache:"no-store"} : {method:"POST",headers:{"Content-Type":"application/json","X-XCode-Client":"1"},body:JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
function toast(message, error=false) {
  $("toast").textContent = message; $("toast").classList.toggle("error", error); $("toast").hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => {$("toast").hidden = true;}, 6000);
}
function timeLabel(value, seconds=false) {
  if (!value) return "—";
  return new Date(value).toLocaleTimeString("zh-CN", {hour12:false,hour:"2-digit",minute:"2-digit",...(seconds?{second:"2-digit"}:{})});
}
function showPage(name) {
  page = name;
  document.querySelectorAll(".page").forEach(p => {p.hidden = p.id !== `page-${name}`;});
  document.querySelectorAll(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.page === name));
  $("page-name").textContent = {workspace:"编程工作台",memory:"长期记忆",extensions:"技能与工具",settings:"模型设置"}[name];
  if (name === "settings" && state) populateSettings();
  if (name === "extensions" && state) renderExtensions();
  if (name === "memory" && state) {renderScopes(); loadMemory();}
}
document.querySelectorAll(".nav-item").forEach(b => b.addEventListener("click", () => showPage(b.dataset.page)));
$("provider-link").addEventListener("click", () => showPage("settings"));
document.querySelectorAll("[data-prompt]").forEach(b => b.addEventListener("click", () => {$("objective").value = b.dataset.prompt; $("objective").focus();}));
$("new-task").addEventListener("click", () => {runId=null;run=null;lastRunJSON="";showPage("workspace");renderRun();renderHistory();$("objective").value="";$("objective").focus();});

function renderHistory() {
  if (!state) return;
  if (!state.runs.length) {$("run-history").innerHTML = '<p class="side-empty">创建第一个任务<br>让想法开始执行。</p>';return;}
  $("run-history").innerHTML = state.runs.map(r => `<button class="history-item ${r.id===runId?"selected":""}" data-run="${escapeHTML(r.id)}"><span class="history-dot ${escapeHTML(r.status)}"></span><span class="history-text">${escapeHTML(r.objective)}<small>${timeLabel(r.created)} · ${names[r.status]||r.status}${r.mode==="demo"?" · DEMO":""}</small></span></button>`).join("");
  $("run-history").querySelectorAll("[data-run]").forEach(b => b.addEventListener("click", async () => {
    runId=b.dataset.run;lastRunJSON="";showPage("workspace");
    try {run=await api(`/api/runs/${runId}`);renderRun();renderHistory();} catch(e){toast(e.message,true);}
  }));
}
async function refreshState(initial=false) {
  if(statePolling) return;
  statePolling=true;
  try {
    state=await api("/api/state");$("connection").textContent="服务已连接";$("connection").classList.remove("offline");
    $("worker-count").innerHTML=`${state.settings.max_workers} <span class="stat-unit">agents</span>`;
    $("skill-count").innerHTML=`${state.skills.filter(s=>s.enabled).length} <span class="stat-unit">skills</span>`;
    $("memory-engine").innerHTML=`SQLite <span class="stat-chip">${state.memory_fts?"FTS5":"LIKE"}</span>`;
    if(initial) {
      $("workspace-path").value=state.default_workspace;
      if(state.runs.length){runId=state.runs[0].id;run=await api(`/api/runs/${runId}`);}
      populateSettings();renderExtensions();renderRun();
    }
    const anyActive=state.runs.some(r=>activeStatuses.has(r.status));
    $("run-button").disabled=anyActive;$("demo-button").disabled=anyActive;
    $("composer-note").textContent=anyActive?"任务正在运行。完成或停止后可创建新任务。":state.settings.has_api_key?"DeepSeek Key 已配置。变更将保留在副本中，等待审阅。":"先填写 DeepSeek Key，或运行无需 Key 的离线演示。";
    renderHistory();
  } catch(e){$("connection").textContent="连接断开";$("connection").classList.add("offline");if(initial)toast("无法连接本地后端，请检查服务是否启动",true);}
  finally{statePolling=false;}
}
async function refreshRun() {
  if(!runId||runPolling) return;
  runPolling=true;const requested=runId;
  try {const next=await api(`/api/runs/${requested}`);if(runId!==requested)return;const serialized=JSON.stringify(next);if(serialized!==lastRunJSON){run=next;lastRunJSON=serialized;renderRun();}}
  catch(e){if(run&&activeStatuses.has(run.status))toast(e.message,true);}
  finally{runPolling=false;}
}

function renderRun() {
  $("run-status").textContent=run?names[run.status]:"等待任务";
  $("run-status").className=`status-tag ${run?run.status:"idle"}`;
  $("cancel-button").hidden=!run||!activeStatuses.has(run.status);
  $("cancel-button").disabled=!!run&&run.status==="cancelling";
  $("run-meta").textContent=run?`${run.mode==="demo"?"离线演示":"DeepSeek"} · ${run.id} · ${run.tokens.toLocaleString()} tokens`:"尚未启动任务";
  const stages=$("pipeline").querySelectorAll(".pipeline-stage");
  let stage=!run?-1:run.status==="planning"?0:["running","cancelling"].includes(run.status)?1:2;
  stages.forEach((s,i)=>{s.classList.toggle("current",i===stage);s.classList.toggle("done",i<stage);});
  if(!run){
    $("agent-area").innerHTML='<div class="empty-orbit"><div class="orbit-center">x</div><span class="orbit-node n1">◇</span><span class="orbit-node n2">⌘</span><span class="orbit-node n3">✓</span></div><h3>一个目标，多路推进</h3><p>规划代理分配任务，执行代理各自工作。<br>依赖关系由调度器统一管理。</p><div class="empty-legend"><span><i class="legend-dot"></i> fork / 规划</span><span><i class="legend-dot teal"></i> leaf / 执行</span></div>';
    $("event-log").innerHTML='<div class="log-placeholder"><span class="mono">&gt;_</span><p>等待调度器启动</p></div>';
    $("changes-content").innerHTML='<div class="empty-mini"><span class="empty-symbol">±</span><p>文件改动将在这里汇总</p><span>对照 diff 审阅，再合并到项目</span></div>';
    $("file-count").textContent="0 FILES";$("review-actions").hidden=true;return;
  }
  $("agent-area").innerHTML=`<div class="agent-list">${run.agents.map(a=>`<div class="agent-card ${escapeHTML(a.status)}"><div class="agent-avatar">${a.status==="completed"?"✓":a.role==="fork"?"◇":"⌘"}</div><div class="agent-body"><div class="agent-heading"><strong>${escapeHTML(a.label)} <small class="role-tag">${a.role}</small></strong><span>${names[a.status]||a.status}</span></div><div class="agent-subtitle">${escapeHTML(a.id)} · ${a.steps} steps${a.depends_on.length?" · 依赖 "+escapeHTML(a.depends_on.join(", ")):" · 无前置依赖"}</div>${a.error?`<div class="agent-error">${escapeHTML(a.error)}</div>`:""}</div></div>`).join("")}</div>`;
  const log=$("event-log"),nearBottom=log.scrollHeight-log.scrollTop-log.clientHeight<45;
  log.innerHTML=run.events.map(e=>`<div class="log-event ${escapeHTML(e.kind)}"><span class="log-time">${timeLabel(e.time,true)}</span><span class="log-agent">${escapeHTML(e.agent)}</span><span class="log-message">${escapeHTML(e.message)}</span></div>`).join("")||'<div class="log-placeholder">正在建立项目快照…</div>';
  if(nearBottom)log.scrollTop=log.scrollHeight;
  $("file-count").textContent=`${run.patches.length} FILES`;
  $("changes-content").innerHTML=(run.error?`<div class="run-error">${escapeHTML(run.error)}</div>`:"")+run.conflicts.map(c=>`<div class="conflict-card"><strong>${escapeHTML(c.path)}</strong><br>${escapeHTML(c.agents.join(" ↔ "))} · ${escapeHTML(c.reason)}</div>`).join("")+run.patches.map((p,i)=>`<button class="file-row" data-patch="${i}"><span class="file-symbol">${p.kind==="added"?"+":p.kind==="deleted"?"−":"±"}</span><span class="file-path">${escapeHTML(p.path)}</span><span class="file-kind ${p.kind}">${{added:"新增",modified:"修改",deleted:"删除"}[p.kind]}</span><span>↗</span></button>`).join("")+(run.verification?.results.length?'<button class="verify-button" id="view-verification">查看验证结果 ↗</button>':"")+(run.patches.length||run.error||run.conflicts.length?"":'<div class="empty-mini"><span class="empty-symbol">±</span><p>尚无文件变更</p><span>执行结束后将汇总代码差异</span></div>');
  $("changes-content").querySelectorAll("[data-patch]").forEach(b=>b.addEventListener("click",()=>showDiff(run.patches[Number(b.dataset.patch)])));
  if($("view-verification"))$("view-verification").addEventListener("click",()=>showText("验证结果",run.verification.results.map(c=>`$ ${c.argv.join(" ")}\nexit: ${c.exit_code}${c.reason?" / "+c.reason:""}\n${c.output}`).join("\n\n"),"VERIFICATION"));
  $("review-actions").hidden=!run.verification;
  $("verification-note").textContent=run.status==="applied"?"已合并到项目":run.verification?.note||"";
  $("apply-button").disabled=run.status!=="ready"||!run.patches.length;
  $("apply-button").textContent=run.status==="applied"?"已合并 ✓":"合并到项目 ↗";
}
function openDialog(title,eyebrow="CODE REVIEW"){$("dialog-title").textContent=title;$("dialog-eyebrow").textContent=eyebrow;$("detail-dialog").showModal();}
function showDiff(patch){$("dialog-content").innerHTML=patch.diff.split("\n").map(line=>`<div class="diff-line ${line.startsWith("+++")||line.startsWith("---")||line.startsWith("@@")?"meta":line.startsWith("+")?"add":line.startsWith("-")?"remove":""}">${escapeHTML(line)||" "}</div>`).join("");openDialog(patch.path);}
function showText(title,text,eyebrow){$("dialog-content").innerHTML='<pre class="raw-content"></pre>';$("dialog-content").firstElementChild.textContent=text;openDialog(title,eyebrow);}
$("dialog-close").addEventListener("click",()=>$("detail-dialog").close());
$("detail-dialog").addEventListener("click",e=>{if(e.target===$("detail-dialog")){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close();}});

async function createRun(mode) {
  const objective=mode==="demo"?"实现带测试与文档的 Python 计算器":""+$("objective").value.trim();
  if(!objective){toast("请先填写任务目标",true);$("objective").focus();return;}
  $("run-button").disabled=true;$("demo-button").disabled=true;
  try {run=await api("/api/runs",{objective,workspace:$("workspace-path").value.trim(),mode,allow_commands:$("allow-commands").checked});runId=run.id;lastRunJSON="";showPage("workspace");renderRun();await refreshState();toast(mode==="demo"?"离线演示已启动：真实并发、文件修改与单元测试":"任务已提交给规划代理");}
  catch(e){toast(e.message,true);await refreshState();}
}
$("run-button").addEventListener("click",()=>createRun("live"));
$("demo-button").addEventListener("click",()=>createRun("demo"));
$("cancel-button").addEventListener("click",async()=>{if(!run)return;try{run=await api(`/api/runs/${run.id}/cancel`,{});renderRun();}catch(e){toast(e.message,true);}});
$("apply-button").addEventListener("click",async()=>{if(!run)return;$("apply-button").disabled=true;try{run=await api(`/api/runs/${run.id}/apply`,{});renderRun();await refreshState();toast(`已合并 ${run.patches.length} 个文件到 ${run.workspace}`);}catch(e){toast(e.message,true);renderRun();}});

function populateSettings(){
  const s=state.settings;
  $("base-url").value=s.base_url;$("model-name").value=s.model;$("key-state").textContent=s.has_api_key?"已配置":"未配置";
  $("api-key").value="";$("clear-key").checked=false;
  for(const [id,key] of [["max-workers","max_workers"],["max-steps","max_steps"],["max-tasks","max_tasks"],["max-depth","max_depth"],["request-timeout","timeout"],["context-budget","context_chars"]])$(id).value=s[key];
  $("command-config").value=JSON.stringify(s.allowed_commands,null,2);
}
$("settings-form").addEventListener("submit",async e=>{
  e.preventDefault();
  try{
    const updates={base_url:$("base-url").value.trim(),model:$("model-name").value.trim(),allowed_commands:JSON.parse($("command-config").value)};
    for(const [id,key] of [["max-workers","max_workers"],["max-steps","max_steps"],["max-tasks","max_tasks"],["max-depth","max_depth"],["request-timeout","timeout"],["context-budget","context_chars"]])updates[key]=Number($(id).value);
    if($("clear-key").checked)updates.api_key="";else if($("api-key").value.trim())updates.api_key=$("api-key").value.trim();
    state.settings=await api("/api/settings",updates);populateSettings();await refreshState();toast("设置已保存，API Key 保存在当前后端进程内存中");
  }catch(e){toast(e.message,true);}
});
function renderExtensions(){
  $("skills-list").innerHTML=state.skills.map((s,i)=>`<section class="panel skill-card"><div class="skill-card-head"><div class="skill-glyph">◇</div><h3>${escapeHTML(s.name)}</h3><button class="toggle ${s.enabled?"enabled":""}" data-skill="${i}" aria-label="${escapeHTML(s.name)} 启用开关" aria-pressed="${s.enabled}" ${s.error?"disabled":""}></button></div><p>${escapeHTML(s.error||s.description)}</p><div class="skill-tags">${(Array.isArray(s.tools)?s.tools:[]).map(t=>`<span>${escapeHTML(t)}</span>`).join("")}</div></section>`).join("")||'<section class="panel memory-card"><p class="form-note">尚无技能。在项目的 skills/&lt;name&gt;/SKILL.md 中添加技能文件。</p></section>';
  $("skills-list").querySelectorAll("[data-skill]").forEach(b=>b.addEventListener("click",async()=>{const s=state.skills[Number(b.dataset.skill)];let disabled=new Set(state.settings.disabled_skills);if(s.enabled)disabled.add(s.name);else disabled.delete(s.name);try{await api("/api/settings",{disabled_skills:[...disabled]});await refreshState();renderExtensions();toast(`${s.name} ${s.enabled?"已禁用":"已启用"}`);}catch(e){toast(e.message,true);}}));
  $("mcp-config").value=JSON.stringify(state.settings.mcp_servers,null,2);
  $("mcp-services").innerHTML=state.settings.mcp_servers.map((c,i)=>`<div class="mcp-service-row"><span>${escapeHTML(c.name)}<small>${escapeHTML(c.transport||"stdio")} · ${(c.allowed_tools||[]).length} 个允许工具</small></span><button class="button secondary small" data-probe="${i}">检测工具列表 ↗</button></div>`).join("");
  $("mcp-services").querySelectorAll("[data-probe]").forEach(b=>b.addEventListener("click",async()=>{const c=state.settings.mcp_servers[Number(b.dataset.probe)];b.disabled=true;b.textContent="检测中…";try{const result=await api("/api/mcp/probe",{name:c.name});showText(`${c.name} · ${result.tools.length} 个工具`,JSON.stringify(result.tools,null,2),"MCP TOOL DISCOVERY");}catch(e){toast(e.message,true);}finally{b.disabled=false;b.textContent="检测工具列表 ↗";}}));
}
$("mcp-save").addEventListener("click",async()=>{try{await api("/api/settings",{mcp_servers:JSON.parse($("mcp-config").value)});await refreshState();renderExtensions();toast("MCP 配置已保存");}catch(e){toast(e.message,true);}});

function renderScopes(){const select=$("memory-scope"),selected=select.value;const scopes=[...new Set(state.runs.map(r=>r.workspace))];select.innerHTML='<option value="">全部项目</option>'+scopes.map(s=>`<option value="${escapeHTML(s)}">${escapeHTML(s)}</option>`).join("");select.value=selected;}
async function loadMemory(){
  try{const q=new URLSearchParams({q:$("memory-query").value,scope:$("memory-scope").value});if(!$("memory-scope").value)q.delete("scope");const results=await api(`/api/memory?${q}`);
    $("memory-results").innerHTML=results.map(r=>`<article class="panel memory-card"><h3>${escapeHTML(r.title)}</h3><pre>${escapeHTML(r.summary)}</pre><div class="memory-card-footer"><span>#${r.id} · ${new Date(r.created).toLocaleString("zh-CN",{hour12:false})} · ${escapeHTML(r.scope)}</span><button class="text-button" data-memory="${r.id}">原始记录 ↗</button></div></article>`).join("")||'<section class="panel memory-card"><div class="empty-mini"><span class="empty-symbol">▤</span><p>没有匹配的记忆</p><span>运行任务后，摘要与原始记录会保存到这里</span></div></section>';
    $("memory-results").querySelectorAll("[data-memory]").forEach(b=>b.addEventListener("click",async()=>{try{const raw=await api(`/api/memory?${q}&raw=1`),item=raw.find(r=>r.id===Number(b.dataset.memory));if(item){let text=item.raw;try{text=JSON.stringify(JSON.parse(text),null,2);}catch{}showText(`记忆 #${item.id}`,text,"RAW RECORD");}}catch(e){toast(e.message,true);}}));
  }catch(e){toast(e.message,true);}
}
$("memory-search-button").addEventListener("click",loadMemory);
$("memory-query").addEventListener("keydown",e=>{if(e.key==="Enter")loadMemory();});
$("memory-scope").addEventListener("change",loadMemory);
refreshState(true);
setInterval(()=>refreshState(),4000);
setInterval(()=>{if(runId&&(!run||activeStatuses.has(run.status)))refreshRun();},900);
