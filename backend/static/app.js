const app = document.getElementById("app");
const BEEPY_UI_BUILD = "email-intelligence-v1-20260807";

const DEFAULT_PREFERENCES = {
  enterToSend: true,
  autoScroll: true,
  showDetails: true,
};

function loadPreferences(){
  try{
    return {...DEFAULT_PREFERENCES,...JSON.parse(localStorage.getItem("mbc_beepy_preferences")||"{}")};
  }catch{
    return {...DEFAULT_PREFERENCES};
  }
}

const state = {
  config:null, account:null, token:null, conversations:[], conversationId:null,
  messages:[], mode:"auto", busy:false, status:null, emailStatus:null,
  page:"chat", projects:[], activeProject:null, projectWorkspace:null, projectCreateOpen:false,
  projectTab:"overview", projectToolboxOpen:false, projectLayoutDraft:null, projectLinkPreview:null, projectBusy:false,
  draft:"",
  collapsed:localStorage.getItem("mbc_beepy_sidebar_collapsed")==="true",
  settingsOpen:false,
  preferences:loadPreferences(),
};

const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
}[c]));

function savePreferences(){
  localStorage.setItem("mbc_beepy_preferences",JSON.stringify(state.preferences));
}

function base64url(bytes){
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"" );
}
async function sha256(text){
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
}
function randomString(length=64){
  const chars="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~";
  const bytes=crypto.getRandomValues(new Uint8Array(length));
  return Array.from(bytes,b=>chars[b%chars.length]).join("");
}
function decodeJwt(token){
  try{return JSON.parse(atob(token.split(".")[1].replace(/-/g,"+").replace(/_/g,"/")))}
  catch{return {}}
}
function engineLabel(value){
  return ({
    "odysseus-rag":"Odysseus RAG · qwen2.5:3b-project",
    "local-qwen-fallback":"Local Qwen fallback",
    "tech-unavailable":"Tech service unavailable",
    "autotask-exact":"Autotask tickets",
    "autotask-hybrid":"Autotask tickets",
    "autotask-no-match":"Autotask · no match",
    "m365-email":"Microsoft 365 Email Intelligence",
    "m365-email-no-match":"Microsoft 365 Email · no match",
    "email-denied":"Email Intelligence · access required",
    "source-clarification":"Source clarification"
  })[value]||value;
}
function markdown(text){
  let s=esc(text);
  s=s.replace(/```([\s\S]*?)```/g,"<pre><code>$1</code></pre>")
     .replace(/^### (.+)$/gm,"<h3>$1</h3>")
     .replace(/^## (.+)$/gm,"<h2>$1</h2>")
     .replace(/^# (.+)$/gm,"<h1>$1</h1>")
     .replace(/\*\*(.+?)\*\*/g,"<strong>$1</strong>")
     .replace(/`([^`]+)`/g,"<code>$1</code>");
  const lines=s.split("\n"); let out="",list=false;
  for(const line of lines){
    const m=line.match(/^\s*[-*]\s+(.+)/);
    if(m){if(!list){out+="<ul>";list=true}out+=`<li>${m[1]}</li>`;continue}
    if(list){out+="</ul>";list=false}
    if(!line.trim())continue;
    out+=/^<h|^<pre/.test(line)?line:`<p>${line}</p>`;
  }
  if(list)out+="</ul>";
  return out;
}

async function loadConfig(){
  state.config=await fetch("/api/public-config").then(r=>r.json());
}
async function beginLogin(){
  const verifier=randomString(96);
  const challenge=base64url(await sha256(verifier));
  sessionStorage.setItem("pkce_verifier",verifier);
  sessionStorage.setItem("oauth_state",randomString(40));
  const params=new URLSearchParams({
    client_id:state.config.clientId,
    response_type:"code",
    redirect_uri:location.origin+"/",
    response_mode:"query",
    scope:"openid profile email User.Read",
    state:sessionStorage.getItem("oauth_state"),
    code_challenge:challenge,
    code_challenge_method:"S256"
  });
  location.href=`https://login.microsoftonline.com/${state.config.tenantId}/oauth2/v2.0/authorize?${params}`;
}
async function handleCallback(){
  const params=new URLSearchParams(location.search);
  if(!params.has("code"))return;
  if(params.get("state")!==sessionStorage.getItem("oauth_state"))throw new Error("OAuth state mismatch.");
  const body=new URLSearchParams({
    client_id:state.config.clientId,
    grant_type:"authorization_code",
    code:params.get("code"),
    redirect_uri:location.origin+"/",
    code_verifier:sessionStorage.getItem("pkce_verifier"),
    scope:"openid profile email User.Read"
  });
  const response=await fetch(`https://login.microsoftonline.com/${state.config.tenantId}/oauth2/v2.0/token`,{
    method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body
  });
  const data=await response.json();
  if(!response.ok)throw new Error(data.error_description||"Microsoft token exchange failed.");
  sessionStorage.setItem("access_token",data.access_token);
  sessionStorage.setItem("id_token",data.id_token||"");
  history.replaceState(null,"",location.pathname);
}
async function api(path,options={}){
  const token=sessionStorage.getItem("access_token");
  const headers={...(options.headers||{}),Authorization:`Bearer ${token}`};
  if(options.body && !(options.body instanceof FormData))headers["Content-Type"]="application/json";
  const response=await fetch(path,{...options,headers});
  if(!response.ok){
    const body=await response.json().catch(()=>({}));
    throw new Error(body.detail||body.error||`HTTP ${response.status}`);
  }
  return response.json();
}
async function activate(){
  const token=sessionStorage.getItem("access_token");
  if(!token)return;
  state.token=token;
  try{state.account=await api("/api/me")}
  catch{sessionStorage.clear();state.token=null;state.account=null}
}
function logout(){sessionStorage.clear();location.reload()}

function updateStatusElement(){
  const statusElement=document.querySelector(".status");
  if(statusElement&&state.status){
    let text=`${Number(state.status.tickets).toLocaleString()} tickets · ${Number(state.status.notes).toLocaleString()} notes`;
    if(state.emailStatus?.authorized){
      text+=` · ${Number(state.emailStatus.messages||0).toLocaleString()} emails`;
    }
    statusElement.textContent=text;
  }
}

async function refreshStatus(){
  if(!state.token)return;
  try{
    const [status,emailStatus]=await Promise.all([
      api("/api/status"),
      api("/api/email/status").catch(()=>state.emailStatus)
    ]);
    state.status=status;
    state.emailStatus=emailStatus;
    updateStatusElement();
  }catch(error){
    console.warn("Status refresh failed:",error);
  }
}

async function refreshConversations(){
  if(!state.token)return;
  state.conversations=await api("/api/conversations");
  renderHistoryOnly();
}

async function loadInitialData(){
  if(!state.token)return;
  const [conversations,status,emailStatus]=await Promise.all([
    api("/api/conversations"),
    api("/api/status"),
    api("/api/email/status").catch(()=>null)
  ]);
  state.conversations=conversations;
  state.status=status;
  state.emailStatus=emailStatus;
}

async function openConversation(id){
  if(state.busy)return;
  const data=await api(`/api/conversations/${id}`);
  state.conversationId=id;
  state.messages=data.messages;
  state.page="chat";
  state.activeProject=null;
  closeConversationMenu();
  render();
}
function newConversation(){
  if(state.busy)return;
  state.conversationId=null;
  state.messages=[];
  closeConversationMenu();
  render();
}

const PROJECT_WIDGETS = [
  ["brief","Project brief"],["beepy","Ask Beepy"],["chat","Team chat"],["notes","Pinned notes"],
  ["tasks","Tasks"],["ideas","Ideas"],["risks","Risks"],["links","Links"],["files","Files"],
  ["decisions","Decisions"],["activity","Activity"]
];

async function refreshProjects(){
  if(!state.token)return;
  state.projects=await api("/api/projects");
}

async function refreshProjectWorkspace(renderAfter=false){
  if(!state.activeProject)return;
  state.projectWorkspace=await api(`/api/projects/${state.activeProject.id}/workspace`);
  if(renderAfter)render();
}

async function reloadActiveProject(renderAfter=true){
  if(!state.activeProject)return;
  const id=state.activeProject.id;
  const [project,workspace]=await Promise.all([
    api(`/api/projects/${id}`),
    api(`/api/projects/${id}/workspace`),
  ]);
  state.activeProject=project;
  state.projectWorkspace=workspace;
  await refreshProjects();
  if(renderAfter)render();
}

async function showProjects(){
  if(state.busy)return;
  state.page="projects";
  state.activeProject=null;
  state.projectWorkspace=null;
  state.projectCreateOpen=false;
  state.projectToolboxOpen=false;
  state.projectLinkPreview=null;
  state.settingsOpen=false;
  closeConversationMenu();
  try{await refreshProjects();render()}catch(error){alert(`Could not load projects: ${error.message}`)}
}

async function openProject(id){
  if(state.busy)return;
  try{
    const [project,workspace]=await Promise.all([
      api(`/api/projects/${id}`),
      api(`/api/projects/${id}/workspace`),
    ]);
    state.activeProject=project;
    state.projectWorkspace=workspace;
    state.projectTab="overview";
    state.projectCreateOpen=false;
    state.projectToolboxOpen=false;
    state.projectLinkPreview=null;
    state.page="projects";
    render();
  }catch(error){alert(`Could not open project: ${error.message}`)}
}

async function createProject(event){
  event?.preventDefault();
  const name=document.getElementById("project-name")?.value?.trim()||"";
  const description=document.getElementById("project-description")?.value?.trim()||"";
  if(!name)return;
  const button=document.getElementById("project-create-submit");
  if(button)button.disabled=true;
  try{
    const created=await api("/api/projects",{method:"POST",body:JSON.stringify({name,description})});
    await refreshProjects();
    await openProject(created.id);
  }catch(error){alert(`Could not create project: ${error.message}`);if(button)button.disabled=false}
}

async function inviteProjectMember(event){
  event?.preventDefault();
  if(!state.activeProject)return;
  const email=document.getElementById("project-invite-email")?.value?.trim()||"";
  const role=document.getElementById("project-invite-role")?.value||"member";
  if(!email)return;
  try{
    await api(`/api/projects/${state.activeProject.id}/invite`,{method:"POST",body:JSON.stringify({email,role})});
    await reloadActiveProject();
  }catch(error){alert(`Could not invite user: ${error.message}`)}
}

async function changeProjectMemberRole(memberId,role,email){
  if(!state.activeProject)return;
  if(!confirm(`Change ${email} to ${role}?`))return;
  try{
    await api(`/api/projects/${state.activeProject.id}/members/${memberId}/role`,{method:"PATCH",body:JSON.stringify({role})});
    await reloadActiveProject();
  }catch(error){alert(`Could not change role: ${error.message}`)}
}

async function removeProjectMember(memberId,email){
  if(!state.activeProject)return;
  if(!confirm(`Remove ${email} from this project?`))return;
  try{
    await api(`/api/projects/${state.activeProject.id}/members/${memberId}`,{method:"DELETE"});
    if(email.toLowerCase()===state.account.email.toLowerCase()){state.activeProject=null;await refreshProjects();render();return}
    await reloadActiveProject();
  }catch(error){alert(`Could not remove member: ${error.message}`)}
}

async function deleteActiveProject(){
  const project=state.activeProject;
  if(!project)return;
  if(!confirm(`Delete project "${project.name}"?\n\nThis permanently deletes the shared workspace, messages, notes, files, and project records.`))return;
  try{
    await api(`/api/projects/${project.id}`,{method:"DELETE"});
    state.activeProject=null;state.projectWorkspace=null;await refreshProjects();render();
  }catch(error){alert(`Could not delete project: ${error.message}`)}
}

function projectNavigate(tab){state.projectTab=tab;state.projectLinkPreview=null;render()}

async function postProjectMessage(event){
  event?.preventDefault();
  const input=document.getElementById("project-chat-input");
  const content=input?.value?.trim()||"";
  if(!content||!state.activeProject)return;
  if(input)input.disabled=true;
  try{
    await api(`/api/projects/${state.activeProject.id}/messages`,{method:"POST",body:JSON.stringify({content})});
    await refreshProjectWorkspace();
    render();
    requestAnimationFrame(()=>{const box=document.querySelector(".project-chat-stream");if(box)box.scrollTop=box.scrollHeight});
  }catch(error){alert(`Could not post message: ${error.message}`);if(input)input.disabled=false}
}

async function askProjectBeepy(event){
  event?.preventDefault();
  const input=document.getElementById("project-beepy-input")||document.getElementById("project-beepy-quick");
  const question=input?.value?.trim()||"";
  if(!question||!state.activeProject||state.projectBusy)return;
  state.projectBusy=true;
  if(input)input.value="";
  render();
  try{
    await api(`/api/projects/${state.activeProject.id}/beepy`,{method:"POST",body:JSON.stringify({question})});
    await refreshProjectWorkspace();
  }catch(error){alert(`Project Beepy could not answer: ${error.message}`)}
  finally{state.projectBusy=false;render()}
}

function closeProjectMessageMenu(){
  document.getElementById("project-message-menu")?.remove();
}
function showProjectMessageMenu(event,messageId){
  event.preventDefault();event.stopPropagation();closeProjectMessageMenu();
  const msg=[...(state.projectWorkspace?.teamMessages||[]),...(state.projectWorkspace?.beepyMessages||[])].find(x=>x.id===messageId);
  if(!msg)return;
  const mine=(msg.authorEmail||"").toLowerCase()===(state.account.email||"").toLowerCase();
  const canManage=!!state.activeProject?.permissions?.manageWorkspace;
  const menu=document.createElement("div");menu.id="project-message-menu";menu.className="context-menu project-message-menu";
  menu.innerHTML=`<div class="context-title">${esc(msg.authorEmail||"Beepy")}</div>
    <button data-pm-action="note">📝 Add to Notes</button>
    <button data-pm-action="task">✓ Create task</button>
    <button data-pm-action="copy">⧉ Copy text</button>
    ${(mine||canManage)&&msg.role!=="assistant"?'<button data-pm-action="delete" class="danger-menu-item">Delete message</button>':""}`;
  document.body.appendChild(menu);
  const rect=menu.getBoundingClientRect();
  menu.style.left=`${Math.min(event.clientX,innerWidth-rect.width-8)}px`;menu.style.top=`${Math.min(event.clientY,innerHeight-rect.height-8)}px`;
  menu.onclick=async e=>{
    const action=e.target.closest("[data-pm-action]")?.dataset.pmAction;if(!action)return;closeProjectMessageMenu();
    try{
      if(action==="copy")await navigator.clipboard.writeText(msg.content||"");
      if(action==="note"){await api(`/api/projects/${state.activeProject.id}/messages/${msg.id}/note`,{method:"POST"});await refreshProjectWorkspace(true)}
      if(action==="task"){await api(`/api/projects/${state.activeProject.id}/tasks`,{method:"POST",body:JSON.stringify({title:(msg.content||"New task").slice(0,180),description:`Created from project chat by ${msg.authorEmail||"Beepy"}`})});await refreshProjectWorkspace(true)}
      if(action==="delete"&&confirm("Delete this project chat message?")){await api(`/api/projects/${state.activeProject.id}/messages/${msg.id}`,{method:"DELETE"});await refreshProjectWorkspace(true)}
    }catch(error){alert(error.message)}
  };
}

async function createProjectNote(event){
  event?.preventDefault();
  const title=document.getElementById("note-title")?.value||"Note";
  const content=document.getElementById("note-content")?.value||"";
  const folder=document.getElementById("note-folder")?.value||"General";
  const pinned=!!document.getElementById("note-pinned")?.checked;
  if(!content.trim())return;
  try{await api(`/api/projects/${state.activeProject.id}/notes`,{method:"POST",body:JSON.stringify({title,content,folder,pinned})});await refreshProjectWorkspace(true)}catch(error){alert(error.message)}
}
async function toggleProjectNote(noteId,pinned){
  try{await api(`/api/projects/${state.activeProject.id}/notes/${noteId}`,{method:"PATCH",body:JSON.stringify({pinned})});await refreshProjectWorkspace(true)}catch(error){alert(error.message)}
}
async function deleteProjectNote(noteId){if(confirm("Delete this note?")){try{await api(`/api/projects/${state.activeProject.id}/notes/${noteId}`,{method:"DELETE"});await refreshProjectWorkspace(true)}catch(error){alert(error.message)}}}

async function createProjectLink(event){
  event?.preventDefault();
  const title=document.getElementById("link-title")?.value||"Link";
  const url=document.getElementById("link-url")?.value||"";
  const description=document.getElementById("link-description")?.value||"";
  try{await api(`/api/projects/${state.activeProject.id}/links`,{method:"POST",body:JSON.stringify({title,url,description})});await refreshProjectWorkspace(true)}catch(error){alert(error.message)}
}
async function deleteProjectLink(id){if(confirm("Remove this link?")){try{await api(`/api/projects/${state.activeProject.id}/links/${id}`,{method:"DELETE"});await refreshProjectWorkspace(true)}catch(error){alert(error.message)}}}

async function createProjectTask(event){
  event?.preventDefault();
  const title=document.getElementById("task-title")?.value||"";
  const description=document.getElementById("task-description")?.value||"";
  const priority=document.getElementById("task-priority")?.value||"normal";
  const assigneeEmail=document.getElementById("task-assignee")?.value||null;
  if(!title.trim())return;
  try{await api(`/api/projects/${state.activeProject.id}/tasks`,{method:"POST",body:JSON.stringify({title,description,priority,assigneeEmail})});await refreshProjectWorkspace(true)}catch(error){alert(error.message)}
}
async function updateProjectTask(id,status){try{await api(`/api/projects/${state.activeProject.id}/tasks/${id}`,{method:"PATCH",body:JSON.stringify({status})});await refreshProjectWorkspace(true)}catch(error){alert(error.message)}}
async function deleteProjectTask(id){if(confirm("Delete this task?")){try{await api(`/api/projects/${state.activeProject.id}/tasks/${id}`,{method:"DELETE"});await refreshProjectWorkspace(true)}catch(error){alert(error.message)}}}

async function createProjectIdea(event){
  event?.preventDefault();
  const title=document.getElementById("idea-title")?.value||"";
  const description=document.getElementById("idea-description")?.value||"";
  if(!title.trim())return;
  try{await api(`/api/projects/${state.activeProject.id}/ideas`,{method:"POST",body:JSON.stringify({title,description})});await refreshProjectWorkspace(true)}catch(error){alert(error.message)}
}
async function voteProjectIdea(id,vote){try{await api(`/api/projects/${state.activeProject.id}/ideas/${id}/vote`,{method:"POST",body:JSON.stringify({vote})});await refreshProjectWorkspace(true)}catch(error){alert(error.message)}}
async function setProjectIdeaStatus(id,status){try{await api(`/api/projects/${state.activeProject.id}/ideas/${id}`,{method:"PATCH",body:JSON.stringify({status})});await refreshProjectWorkspace(true)}catch(error){alert(error.message)}}
async function deleteProjectIdea(id){if(confirm("Delete this idea?")){try{await api(`/api/projects/${state.activeProject.id}/ideas/${id}`,{method:"DELETE"});await refreshProjectWorkspace(true)}catch(error){alert(error.message)}}}

async function createProjectDecision(event){
  event?.preventDefault();
  const title=document.getElementById("decision-title")?.value||"";
  const decision=document.getElementById("decision-text")?.value||"";
  const rationale=document.getElementById("decision-rationale")?.value||"";
  if(!title.trim()||!decision.trim())return;
  try{await api(`/api/projects/${state.activeProject.id}/decisions`,{method:"POST",body:JSON.stringify({title,decision,rationale})});await refreshProjectWorkspace(true)}catch(error){alert(error.message)}
}
async function deleteProjectDecision(id){if(confirm("Delete this decision?")){try{await api(`/api/projects/${state.activeProject.id}/decisions/${id}`,{method:"DELETE"});await refreshProjectWorkspace(true)}catch(error){alert(error.message)}}}

async function createProjectRisk(event){
  event?.preventDefault();
  const title=document.getElementById("risk-title")?.value||"";
  const impact=document.getElementById("risk-impact")?.value||"medium";
  const likelihood=document.getElementById("risk-likelihood")?.value||"medium";
  const mitigation=document.getElementById("risk-mitigation")?.value||"";
  if(!title.trim())return;
  try{await api(`/api/projects/${state.activeProject.id}/risks`,{method:"POST",body:JSON.stringify({title,impact,likelihood,mitigation})});await refreshProjectWorkspace(true)}catch(error){alert(error.message)}
}
async function closeProjectRisk(id){try{await api(`/api/projects/${state.activeProject.id}/risks/${id}`,{method:"PATCH",body:JSON.stringify({status:"closed"})});await refreshProjectWorkspace(true)}catch(error){alert(error.message)}}
async function deleteProjectRisk(id){if(confirm("Delete this risk?")){try{await api(`/api/projects/${state.activeProject.id}/risks/${id}`,{method:"DELETE"});await refreshProjectWorkspace(true)}catch(error){alert(error.message)}}}

async function uploadProjectFile(event){
  event?.preventDefault();
  const input=document.getElementById("project-file-input");
  const file=input?.files?.[0];if(!file)return;
  const data=new FormData();data.append("file",file);
  try{await api(`/api/projects/${state.activeProject.id}/files`,{method:"POST",body:data});await refreshProjectWorkspace(true)}catch(error){alert(`Upload failed: ${error.message}`)}
}
async function downloadProjectFile(fileId,filename){
  try{
    const token=sessionStorage.getItem("access_token");
    const response=await fetch(`/api/projects/${state.activeProject.id}/files/${fileId}/download`,{headers:{Authorization:`Bearer ${token}`}});
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    const blob=await response.blob();const url=URL.createObjectURL(blob);const a=document.createElement("a");a.href=url;a.download=filename||"download";document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),5000);
  }catch(error){alert(`Download failed: ${error.message}`)}
}
async function deleteProjectFile(id){if(confirm("Delete this project file?")){try{await api(`/api/projects/${state.activeProject.id}/files/${id}`,{method:"DELETE"});await refreshProjectWorkspace(true)}catch(error){alert(error.message)}}}

function openProjectToolbox(){
  state.projectToolboxOpen=true;
  state.projectLayoutDraft=[...(state.projectWorkspace?.settings?.layout||PROJECT_WIDGETS.map(x=>x[0]))];
  render();
}
function toggleLayoutWidget(id,enabled){
  let layout=[...(state.projectLayoutDraft||[])];
  if(enabled&&!layout.includes(id))layout.push(id);
  if(!enabled)layout=layout.filter(x=>x!==id);
  state.projectLayoutDraft=layout;render();
}
function moveLayoutWidget(id,direction){
  const layout=[...(state.projectLayoutDraft||[])];const i=layout.indexOf(id);if(i<0)return;const j=i+direction;if(j<0||j>=layout.length)return;[layout[i],layout[j]]=[layout[j],layout[i]];state.projectLayoutDraft=layout;render();
}
async function saveProjectToolbox(event){
  event?.preventDefault();
  const name=document.getElementById("toolbox-project-name")?.value?.trim();
  const description=document.getElementById("toolbox-project-description")?.value??"";
  const status=document.getElementById("toolbox-project-status")?.value||"active";
  const clientName=document.getElementById("toolbox-client-name")?.value||"";
  try{
    await api(`/api/projects/${state.activeProject.id}`,{method:"PATCH",body:JSON.stringify({name,description})});
    await api(`/api/projects/${state.activeProject.id}/workspace/settings`,{method:"PATCH",body:JSON.stringify({status,clientName,layout:state.projectLayoutDraft||[]})});
    state.projectToolboxOpen=false;await reloadActiveProject();
  }catch(error){alert(`Could not save toolbox changes: ${error.message}`)}
}


async function clearAllChats(){
  if(state.busy){
    alert("Wait for the current response to finish before clearing chats.");
    return;
  }
  const okay=confirm("Delete all of your Beepy chats?\n\nThis permanently removes your conversation history and cannot be undone.");
  if(!okay)return;
  try{
    const result=await api("/api/conversations",{method:"DELETE"});
    state.conversations=[];
    state.conversationId=null;
    state.messages=[];
    state.settingsOpen=false;
    state.page="chat";
    state.activeProject=null;
    state.draft="";
    render();
    if(result.deleted)console.info(`Deleted ${result.deleted} chats.`);
  }catch(error){
    alert(`Could not clear chats: ${error.message}`);
  }
}

function historyMarkup(){
  return state.conversations.map(c=>
    `<button class="${c.id===state.conversationId?"selected":""}" data-conv="${c.id}" title="${esc(c.title)}">${esc(c.title)}</button>`
  ).join("");
}

function bindHistoryEvents(root=document){
  root.querySelectorAll("[data-conv]").forEach(button=>{
    button.onclick=()=>openConversation(button.dataset.conv);
    button.oncontextmenu=event=>{
      event.preventDefault();
      event.stopPropagation();
      showConversationMenu(event,button.dataset.conv);
    };
  });
}

function renderHistoryOnly(){
  const history=document.querySelector(".history");
  if(!history)return;
  const oldScrollTop=history.scrollTop;
  history.innerHTML=historyMarkup();
  history.scrollTop=oldScrollTop;
  bindHistoryEvents(history);
}

function closeConversationMenu(){
  document.getElementById("conversation-context-menu")?.remove();
}

function showConversationMenu(event,id){
  closeConversationMenu();
  const conversation=state.conversations.find(c=>String(c.id)===String(id));
  if(!conversation)return;

  const menu=document.createElement("div");
  menu.id="conversation-context-menu";
  menu.className="context-menu";
  menu.innerHTML=`
    <div class="context-title" title="${esc(conversation.title)}">${esc(conversation.title)}</div>
    <button type="button" class="context-delete">⌫ <span>Delete chat</span></button>
  `;
  document.body.appendChild(menu);

  const width=210;
  const height=82;
  menu.style.left=`${Math.max(8,Math.min(event.clientX,window.innerWidth-width-8))}px`;
  menu.style.top=`${Math.max(8,Math.min(event.clientY,window.innerHeight-height-8))}px`;

  menu.querySelector(".context-delete").onclick=async()=>{
    closeConversationMenu();
    const okay=confirm(`Delete "${conversation.title}"?\n\nThis cannot be undone.`);
    if(!okay)return;
    try{
      await api(`/api/conversations/${id}`,{method:"DELETE"});
      if(String(state.conversationId)===String(id)){
        state.conversationId=null;
        state.messages=[];
        await refreshConversations();
        render();
      }else{
        await refreshConversations();
      }
    }catch(error){
      alert(`Could not delete chat: ${error.message}`);
    }
  };

  setTimeout(()=>document.addEventListener("click",closeConversationMenu,{once:true}),0);
}

function isChatNearBottom(){
  const chat=document.querySelector(".chat");
  if(!chat)return true;
  return chat.scrollHeight-chat.scrollTop-chat.clientHeight<140;
}

function maybeFollowResponse(wasNearBottom=true){
  if(!state.preferences.autoScroll||!wasNearBottom)return;
  const chat=document.querySelector(".chat");
  if(chat)chat.scrollTop=chat.scrollHeight;
}

function messageExtrasMarkup(message){
  const sourceRows=(message.sources||[]).map(source=>{
    if(source.sourceType==="email"){
      const label=`✉ ${source.subject||source.title||"Email"}`;
      const meta=[source.sender,source.sentDate?new Date(source.sentDate).toLocaleDateString():""].filter(Boolean).join(" · ");
      const inner=`<span class="email-source-title">${esc(label)}</span>${meta?`<span class="email-source-meta">${esc(meta)}</span>`:""}`;
      return source.url
        ? `<a class="source-chip email-source" href="${esc(source.url)}" target="_blank" rel="noopener">${inner}</a>`
        : `<span class="source-chip email-source">${inner}</span>`;
    }
    const label=source.ticketNumber||source.title;
    if(!label)return "";
    return source.url
      ? `<a class="source-chip ticket-source" href="${esc(source.url)}" target="_blank" rel="noopener">🎫 ${esc(label)}</a>`
      : `<span class="source-chip ticket-source">🎫 ${esc(label)}</span>`;
  }).filter(Boolean).join("");
  const sources=sourceRows?`<div class="sources source-chips">${sourceRows}</div>`:"";
  const engine=message.engine
    ? `<div class="engine">${esc(engineLabel(message.engine))}${message.elapsedMs?` · ${(message.elapsedMs/1000).toFixed(1)}s`:""}</div>`
    : "";
  return `${sources}${engine}`;
}

function messageMarkup(message,index){
  return `<article class="message ${message.role}" data-message-index="${index}">
    <div class="avatar">${message.role==="assistant"?"B":esc((state.account.name||"U")[0])}</div>
    <div class="bubble">
      <div class="message-content">${message.role==="assistant"?markdown(message.content):esc(message.content)}</div>
      <div class="message-extras">${messageExtrasMarkup(message)}</div>
    </div>
  </article>`;
}

function updateMessageElement(index){
  const message=state.messages[index];
  const article=document.querySelector(`[data-message-index="${index}"]`);
  if(!message||!article)return;

  const wasNearBottom=isChatNearBottom();
  const content=article.querySelector(".message-content");
  const extras=article.querySelector(".message-extras");
  if(content)content.innerHTML=message.role==="assistant"?markdown(message.content):esc(message.content);
  if(extras)extras.innerHTML=messageExtrasMarkup(message);

  if(message.content)document.querySelector(".typing-message")?.remove();
  maybeFollowResponse(wasNearBottom);
}

async function send(){
  const box=document.getElementById("question");
  const q=(box?.value||"").trim();
  if(!q||state.busy)return;

  box.value="";
  state.draft="";
  state.messages.push({role:"user",content:q,sources:[]});
  const assistantIndex=state.messages.length;
  state.messages.push({role:"assistant",content:"",sources:[],engine:""});
  state.busy=true;
  render();

  try{
    const response=await fetch("/api/chat/stream",{
      method:"POST",
      headers:{
        "Content-Type":"application/json",
        Authorization:`Bearer ${sessionStorage.getItem("access_token")}`
      },
      body:JSON.stringify({question:q,mode:state.mode,conversationId:state.conversationId})
    });
    if(!response.ok){
      const body=await response.json().catch(()=>({}));
      throw new Error(body.detail||body.error||`HTTP ${response.status}`);
    }

    const reader=response.body.getReader();
    const decoder=new TextDecoder();
    let buffer="";
    while(true){
      const {value,done}=await reader.read();
      if(done)break;
      buffer+=decoder.decode(value,{stream:true});
      const events=buffer.split("\n\n");
      buffer=events.pop()||"";
      for(const raw of events){
        let eventName="message",dataText="";
        for(const line of raw.split("\n")){
          if(line.startsWith("event:"))eventName=line.slice(6).trim();
          if(line.startsWith("data:"))dataText+=line.slice(5).trim();
        }
        if(!dataText)continue;
        const payload=JSON.parse(dataText);
        const message=state.messages[assistantIndex];
        if(eventName==="meta"){
          state.conversationId=payload.conversationId;
          message.sources=payload.sources||[];
          message.engine=payload.engine||"";
        }else if(eventName==="token"){
          message.content+=(payload.text||"");
        }else if(eventName==="replace"){
          message.content=payload.text||"";
        }else if(eventName==="done"){
          message.elapsedMs=payload.elapsedMs;
        }
        updateMessageElement(assistantIndex);
      }
    }
    await refreshConversations();
  }catch(error){
    state.messages[assistantIndex].content=`**Error:** ${error.message}`;
    updateMessageElement(assistantIndex);
  }finally{
    state.busy=false;
    document.querySelector(".typing-message")?.remove();
    updateMessageElement(assistantIndex);
    document.getElementById("question")?.focus({preventScroll:true});
  }
}

function renderLogin(){
  app.innerHTML=`<main class="login-page"><section class="login-card">
    <h1>Sign in to Beepy</h1>
    <p>Private access through Microsoft Entra ID and Tailscale.</p>
    <button id="login" class="microsoft-button"><span class="ms-logo"><i></i><i></i><i></i><i></i></span>Sign in with Microsoft</button>
  </section></main>`;
  document.getElementById("login").onclick=beginLogin;
}

function settingsMarkup(){
  if(!state.settingsOpen)return "";
  return `<div class="settings-backdrop" id="settings-backdrop">
    <section class="settings-panel" role="dialog" aria-modal="true" aria-label="Beepy settings">
      <div class="settings-title-row"><div><h3>Settings</h3><p>Customize Beepy on this browser.</p></div><button id="settings-close" class="settings-close" aria-label="Close settings">×</button></div>
      <div class="settings-group">
        <label class="setting-row"><span><b>Enter sends message</b><small>Use Shift + Enter for a new line.</small></span><input type="checkbox" data-setting="enterToSend" ${state.preferences.enterToSend?"checked":""}></label>
        <label class="setting-row"><span><b>Auto-scroll replies</b><small>Follow new response text while you are near the bottom.</small></span><input type="checkbox" data-setting="autoScroll" ${state.preferences.autoScroll?"checked":""}></label>
        <label class="setting-row"><span><b>Show response details</b><small>Show the engine and response time under answers.</small></span><input type="checkbox" data-setting="showDetails" ${state.preferences.showDetails?"checked":""}></label>
        <label class="setting-row"><span><b>Compact sidebar</b><small>Keep the conversation sidebar collapsed.</small></span><input type="checkbox" data-setting="collapsed" ${state.collapsed?"checked":""}></label>
      </div>
      <div class="settings-account"><span>Signed in as</span><b>${esc(state.account.email)}</b></div>
      <div class="settings-email-intelligence">
        <div><b>✉ Email Intelligence</b><small>${state.emailStatus?.authorized?`${Number(state.emailStatus.messages||0).toLocaleString()} indexed messages across ${Number(state.emailStatus.mailboxes||0).toLocaleString()} mailboxes.`:"Tenant-wide email search is restricted to approved Beepy users."}</small></div>
        <span class="email-status-pill ${state.emailStatus?.authorized?"ready":"locked"}">${state.emailStatus?.authorized?(state.emailStatus?.configured?"Ready":"Setup needed"):"Restricted"}</span>
      </div>
      <div class="settings-danger">
        <div><b>Danger zone</b><small>Permanently remove all of your Beepy conversation history.</small></div>
        <button id="settings-clear-chats" class="settings-clear-chats">Clear all chats</button>
      </div>
      <button id="settings-logout" class="settings-logout">⇥ Sign out</button>
    </section>
  </div>`;
}

function bindSettingsEvents(){
  document.getElementById("settings")?.addEventListener("click",()=>{
    state.settingsOpen=true;
    render();
  });
  document.getElementById("settings-close")?.addEventListener("click",()=>{
    state.settingsOpen=false;
    render();
  });
  document.getElementById("settings-backdrop")?.addEventListener("click",event=>{
    if(event.target.id==="settings-backdrop"){
      state.settingsOpen=false;
      render();
    }
  });
  document.getElementById("settings-clear-chats")?.addEventListener("click",clearAllChats);
  document.getElementById("settings-logout")?.addEventListener("click",logout);
  document.querySelectorAll("[data-setting]").forEach(input=>{
    input.addEventListener("change",event=>{
      const key=event.target.dataset.setting;
      const value=event.target.checked;
      if(key==="collapsed"){
        state.collapsed=value;
        localStorage.setItem("mbc_beepy_sidebar_collapsed",String(value));
        document.querySelector(".app")?.classList.toggle("collapsed",value);
      }else{
        state.preferences[key]=value;
        savePreferences();
        if(key==="showDetails")document.querySelector(".app")?.classList.toggle("hide-details",!value);
      }
    });
  });
}

function formatWhen(value){
  if(!value)return "";
  try{return new Intl.DateTimeFormat(undefined,{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}).format(new Date(value))}catch{return ""}
}
function initials(email){return (String(email||"?").split("@")[0].split(/[._-]/).map(x=>x[0]).join("").slice(0,2)||"?").toUpperCase()}
function roleLabel(role){return role==="owner"?"Owner":role==="admin"?"Admin":"Member"}
function healthLabel(value){return value==="healthy"?"Healthy":value==="at-risk"?"At risk":"Needs attention"}
function projectEmpty(icon,title,text){return `<div class="project-empty-state"><div>${icon}</div><b>${esc(title)}</b><span>${esc(text)}</span></div>`}

function projectOverviewWidget(id){
  const p=state.activeProject,w=state.projectWorkspace||{};
  const notes=w.notes||[],tasks=w.tasks||[],ideas=w.ideas||[],risks=w.risks||[],links=w.links||[],files=w.files||[],decisions=w.decisions||[],activity=w.activity||[],chat=w.teamMessages||[];
  if(id==="brief")return `<article class="pw-card pw-wide"><div class="pw-card-title"><span>Project brief</span><span class="project-status status-${esc(w.settings?.status||"active")}">${esc(w.settings?.status||"active")}</span></div><h2>${esc(p.name)}</h2><p class="pw-description">${esc(p.description||"No project description yet. Use Toolbox to add one.")}</p><div class="pw-metrics"><div><b>${esc(w.settings?.clientName||"Not set")}</b><span>Client</span></div><div><b>${p.memberCount}</b><span>People</span></div><div><b>${w.health?.openTasks||0}</b><span>Open tasks</span></div><div><b>${w.health?.openRisks||0}</b><span>Open risks</span></div><div><b class="health-${esc(w.health?.state||"healthy")}">${healthLabel(w.health?.state)}</b><span>Health</span></div></div></article>`;
  if(id==="beepy")return `<article class="pw-card pw-wide"><div class="pw-card-title"><span>🤖 Ask Beepy</span><button data-project-tab="beepy">Open chat →</button></div><p>Brainstorm architecture, challenge assumptions, turn project facts into an implementation plan, or ask what the team should verify next.</p><form id="project-beepy-quick-form" class="pw-quick-beepy"><textarea id="project-beepy-quick" placeholder="Ask Beepy about this project…"></textarea><button ${state.projectBusy?"disabled":""}>${state.projectBusy?"Thinking…":"Ask Beepy"}</button></form></article>`;
  if(id==="chat")return `<article class="pw-card"><div class="pw-card-title"><span>💬 Team chat</span><button data-project-tab="chat">Open →</button></div>${chat.length?`<div class="pw-mini-list">${chat.slice(-4).map(x=>`<div><b>${esc(x.authorEmail.split("@")[0])}</b><span>${esc(x.content.slice(0,130))}</span></div>`).join("")}</div>`:projectEmpty("💬","No messages yet","Start the shared project conversation.")}</article>`;
  if(id==="notes")return `<article class="pw-card"><div class="pw-card-title"><span>📝 Notes</span><button data-project-tab="notes">Open →</button></div>${notes.length?`<div class="pw-mini-list">${notes.filter(x=>x.pinned).concat(notes.filter(x=>!x.pinned)).slice(0,5).map(x=>`<div><b>${x.pinned?"📌 ":""}${esc(x.title)}</b><span>${esc(x.folder)}</span></div>`).join("")}</div>`:projectEmpty("📝","No notes","Save team-chat messages or create project notes.")}</article>`;
  if(id==="tasks")return `<article class="pw-card"><div class="pw-card-title"><span>✓ Tasks</span><button data-project-tab="tasks">Open →</button></div>${tasks.length?`<div class="pw-mini-list">${tasks.filter(x=>!['done','completed'].includes(x.status)).slice(0,5).map(x=>`<div><b>${esc(x.title)}</b><span>${esc(x.status)} · ${esc(x.assigneeEmail||"Unassigned")}</span></div>`).join("")}</div>`:projectEmpty("✓","No tasks","Turn project ideas into assigned work.")}</article>`;
  if(id==="ideas")return `<article class="pw-card"><div class="pw-card-title"><span>💡 Ideas</span><button data-project-tab="ideas">Open →</button></div>${ideas.length?`<div class="pw-mini-list">${ideas.slice(0,5).map(x=>`<div><b>${esc(x.title)}</b><span>${esc(x.status)} · score ${x.score}</span></div>`).join("")}</div>`:projectEmpty("💡","No ideas","Capture possible solutions before they get lost in chat.")}</article>`;
  if(id==="risks")return `<article class="pw-card"><div class="pw-card-title"><span>⚠ Risks</span><button data-project-tab="risks">Open →</button></div>${risks.length?`<div class="pw-mini-list">${risks.filter(x=>x.status!=="closed").slice(0,5).map(x=>`<div><b>${esc(x.title)}</b><span>${esc(x.impact)} impact · ${esc(x.likelihood)} likelihood</span></div>`).join("")}</div>`:projectEmpty("⚠","No documented risks","Track blockers before deployment day.")}</article>`;
  if(id==="links")return `<article class="pw-card"><div class="pw-card-title"><span>🔗 Links</span><button data-project-tab="links">Open →</button></div>${links.length?`<div class="pw-mini-list">${links.slice(0,5).map(x=>`<div><b>${esc(x.title)}</b><span>${esc(x.url)}</span></div>`).join("")}</div>`:projectEmpty("🔗","No links","Add vendor portals, docs, tickets, and references.")}</article>`;
  if(id==="files")return `<article class="pw-card"><div class="pw-card-title"><span>📁 Files</span><button data-project-tab="files">Open →</button></div>${files.length?`<div class="pw-mini-list">${files.slice(0,5).map(x=>`<div><b>${esc(x.filename)}</b><span>${Math.max(1,Math.round((x.sizeBytes||0)/1024))} KB</span></div>`).join("")}</div>`:projectEmpty("📁","No files","Drop screenshots, PDFs, configs, logs, and project docs here.")}</article>`;
  if(id==="decisions")return `<article class="pw-card"><div class="pw-card-title"><span>◆ Decisions</span><button data-project-tab="decisions">Open →</button></div>${decisions.length?`<div class="pw-mini-list">${decisions.slice(0,4).map(x=>`<div><b>${esc(x.title)}</b><span>${esc(x.decision.slice(0,120))}</span></div>`).join("")}</div>`:projectEmpty("◆","No decisions yet","Record what the team decided and why.")}</article>`;
  if(id==="activity")return `<article class="pw-card"><div class="pw-card-title"><span>🕘 Activity</span><button data-project-tab="activity">Open →</button></div>${activity.length?`<div class="pw-mini-list">${activity.slice(0,5).map(x=>`<div><b>${esc(x.actorEmail.split("@")[0])}</b><span>${esc(x.action)} · ${formatWhen(x.createdAt)}</span></div>`).join("")}</div>`:projectEmpty("🕘","Quiet so far","Workspace changes will appear here.")}</article>`;
  return "";
}

function projectOverviewMarkup(){
  const layout=state.projectWorkspace?.settings?.layout||PROJECT_WIDGETS.map(x=>x[0]);
  return `<div class="project-overview-grid">${layout.map(projectOverviewWidget).join("")}</div>`;
}

function projectBeepyMarkup(){
  const msgs=state.projectWorkspace?.beepyMessages||[];
  return `<div class="project-section-head"><div><h2>Project Beepy</h2><p>Beepy receives project notes, decisions, tasks, risks, links, recent team chat, and Odysseus RAG context.</p></div><span class="ai-live-pill">● Odysseus RAG</span></div>
    <div class="project-beepy-shell"><div class="project-beepy-stream">${msgs.length?msgs.map(x=>`<article class="project-ai-message ${x.role}"><div class="project-ai-avatar">${x.role==="assistant"?"B":esc(initials(x.authorEmail))}</div><div><div class="project-ai-meta">${x.role==="assistant"?"Beepy":esc(x.authorEmail)} · ${formatWhen(x.createdAt)}</div><div class="project-ai-content" data-project-message="${x.id}">${markdown(x.content)}</div></div></article>`).join(""):projectEmpty("🤖","Project Beepy is ready","Ask it to review the plan, challenge an idea, find risks, or build a deployment checklist.")}${state.projectBusy?'<article class="project-ai-message assistant"><div class="project-ai-avatar">B</div><div class="project-ai-content project-thinking">Beepy is reviewing the project…</div></article>':""}</div>
    <form id="project-beepy-form" class="project-beepy-composer"><textarea id="project-beepy-input" placeholder="Ask Beepy about this project…"></textarea><button ${state.projectBusy?"disabled":""}>➤</button></form></div>`;
}

function projectChatMarkup(){
  const msgs=state.projectWorkspace?.teamMessages||[];
  return `<div class="project-section-head"><div><h2>Team Chat</h2><p>Persistent project room for ideas, handoffs, questions, and technical discussion. Right-click a message to save it as a Note or Task.</p></div><span class="persist-pill">∞ Persistent</span></div>
    <div class="project-chat-shell"><div class="project-chat-stream">${msgs.length?msgs.map(x=>`<article class="project-chat-message" data-project-message="${x.id}"><div class="project-chat-avatar">${esc(initials(x.authorEmail))}</div><div class="project-chat-body"><div class="project-chat-meta"><b>${esc(x.authorEmail)}</b><span>${formatWhen(x.createdAt)}</span></div><div>${markdown(x.content)}</div></div></article>`).join(""):projectEmpty("💬","Start the conversation","This chat stays with the project for the whole team.")}</div>
    <form id="project-chat-form" class="project-chat-composer"><textarea id="project-chat-input" placeholder="Message the project team…  @Beepy support can be added later here too."></textarea><button>Send</button></form></div>`;
}

function projectNotesMarkup(){
  const notes=state.projectWorkspace?.notes||[];
  const groups={};notes.forEach(x=>(groups[x.folder||"General"]??=[]).push(x));
  return `<div class="project-section-head"><div><h2>Notes</h2><p>Permanent project knowledge. Chat and Beepy messages can be saved here from their right-click menu.</p></div></div>
    <form id="project-note-form" class="project-inline-form form-grid-2"><label>Title<input id="note-title" placeholder="DNS cutover notes"></label><label>Folder<input id="note-folder" placeholder="General"></label><label class="span-2">Note<textarea id="note-content" placeholder="Write something the team should remember…"></textarea></label><label class="check-line"><input id="note-pinned" type="checkbox"> Pin to overview</label><button class="primary-action">Add note</button></form>
    <div class="note-folders">${Object.entries(groups).map(([folder,items])=>`<section class="note-folder"><h3>📂 ${esc(folder)} <span>${items.length}</span></h3><div class="note-grid">${items.map(x=>`<article class="note-card ${x.pinned?"pinned":""}"><div class="note-card-head"><b>${x.pinned?"📌 ":""}${esc(x.title)}</b><span>${formatWhen(x.updatedAt)}</span></div><div>${markdown(x.content)}</div><footer><span>${esc(x.createdByEmail)}</span><div><button data-note-pin="${x.id}" data-pinned="${x.pinned?"1":"0"}">${x.pinned?"Unpin":"Pin"}</button><button data-note-delete="${x.id}" class="danger-text">Delete</button></div></footer></article>`).join("")}</div></section>`).join("")||projectEmpty("📝","No notes yet","Create one or save a useful chat message as a note.")}</div>`;
}

function projectFilesMarkup(){
  const files=state.projectWorkspace?.files||[];
  return `<div class="project-section-head"><div><h2>Files</h2><p>Shared project attachments use persistent project upload storage.</p></div></div>
    <form id="project-file-form" class="project-upload-box"><div>📁</div><b>Upload a project file</b><span>PDFs, screenshots, logs, configs, documents and other project material · 25 MB max</span><input id="project-file-input" type="file" required><button class="primary-action">Upload</button></form>
    <div class="project-file-list">${files.map(x=>`<article class="project-file-row"><div class="file-icon">${/image/.test(x.contentType||"")?"🖼":"📄"}</div><div><b>${esc(x.filename)}</b><span>${Math.max(1,Math.round((x.sizeBytes||0)/1024))} KB · ${esc(x.uploadedByEmail)} · ${formatWhen(x.createdAt)}</span></div><div><button data-file-download="${x.id}" data-filename="${esc(x.filename)}">Download</button><button data-file-delete="${x.id}" class="danger-text">Delete</button></div></article>`).join("")||projectEmpty("📁","No files uploaded","Keep project evidence, diagrams, logs, and documentation together.")}</div>`;
}

function projectLinksMarkup(){
  const links=state.projectWorkspace?.links||[];
  return `<div class="project-section-head"><div><h2>Links</h2><p>Save vendor portals, documentation, dashboards, Autotask tickets, and research. Use Embed for sites that allow iframe display.</p></div></div>
    <form id="project-link-form" class="project-inline-form form-grid-2"><label>Title<input id="link-title" placeholder="SonicWall documentation"></label><label>URL<input id="link-url" placeholder="https://…" required></label><label class="span-2">Description<input id="link-description" placeholder="Why this link matters to the project"></label><button class="primary-action">Add link</button></form>
    <div class="project-link-grid">${links.map(x=>`<article class="project-link-card"><div class="link-glyph">↗</div><div><b>${esc(x.title)}</b><p>${esc(x.description||x.url)}</p><span>${esc(x.url)}</span></div><footer><a href="${esc(x.url)}" target="_blank" rel="noopener">Open</a><button data-link-preview="${x.id}">Embed</button><button data-link-delete="${x.id}" class="danger-text">Delete</button></footer></article>`).join("")||projectEmpty("🔗","No links","Add the places your team keeps opening during this project.")}</div>`;
}

function projectTasksMarkup(){
  const tasks=state.projectWorkspace?.tasks||[];const members=state.activeProject?.members||[];
  return `<div class="project-section-head"><div><h2>Tasks</h2><p>Turn ideas and project-chat items into actionable work.</p></div></div>
    <form id="project-task-form" class="project-inline-form form-grid-3"><label>Task<input id="task-title" placeholder="Verify backup before cutover"></label><label>Priority<select id="task-priority"><option>normal</option><option>high</option><option>critical</option><option>low</option></select></label><label>Assign to<select id="task-assignee"><option value="">Unassigned</option>${members.map(x=>`<option value="${esc(x.email)}">${esc(x.email)}</option>`).join("")}</select></label><label class="span-3">Details<input id="task-description" placeholder="Acceptance criteria or steps"></label><button class="primary-action">Create task</button></form>
    <div class="project-task-board">${["open","in-progress","blocked","done"].map(status=>`<section class="task-column"><h3>${esc(status.replace("-"," "))}<span>${tasks.filter(x=>x.status===status).length}</span></h3>${tasks.filter(x=>x.status===status).map(x=>`<article class="task-card priority-${esc(x.priority)}"><b>${esc(x.title)}</b><p>${esc(x.description||"")}</p><span>${esc(x.assigneeEmail||"Unassigned")}</span><div><select data-task-status="${x.id}"><option value="open" ${x.status==="open"?"selected":""}>Open</option><option value="in-progress" ${x.status==="in-progress"?"selected":""}>In progress</option><option value="blocked" ${x.status==="blocked"?"selected":""}>Blocked</option><option value="done" ${x.status==="done"?"selected":""}>Done</option></select><button data-task-delete="${x.id}" class="danger-text">Delete</button></div></article>`).join("")||'<div class="column-empty">Nothing here</div>'}</section>`).join("")}</div>`;
}

function projectIdeasMarkup(){
  const ideas=state.projectWorkspace?.ideas||[];const canManage=state.activeProject?.permissions?.manageWorkspace;
  return `<div class="project-section-head"><div><h2>Idea Lab</h2><p>Propose approaches, vote, discuss, and move the best ideas toward implementation.</p></div><span class="idea-pill">💡 Brainstorm mode</span></div>
    <form id="project-idea-form" class="project-inline-form"><label>Idea<input id="idea-title" placeholder="Separate camera traffic into its own VLAN"></label><label>Why / how<textarea id="idea-description" placeholder="Benefits, tradeoffs, assumptions, dependencies…"></textarea></label><button class="primary-action">Propose idea</button></form>
    <div class="idea-grid">${ideas.map(x=>`<article class="idea-card"><header><span class="idea-status">${esc(x.status)}</span><span>Score <b>${x.score}</b></span></header><h3>${esc(x.title)}</h3><p>${esc(x.description)}</p><footer><div><button data-idea-vote="${x.id}" data-vote="1" class="${x.myVote===1?"voted":""}">▲</button><button data-idea-vote="${x.id}" data-vote="-1" class="${x.myVote===-1?"voted":""}">▼</button></div>${canManage?`<select data-idea-status="${x.id}"><option value="discussing" ${x.status==="discussing"?"selected":""}>Discussing</option><option value="reviewing" ${x.status==="reviewing"?"selected":""}>Reviewing</option><option value="approved" ${x.status==="approved"?"selected":""}>Approved</option><option value="implementation" ${x.status==="implementation"?"selected":""}>Implementation</option><option value="completed" ${x.status==="completed"?"selected":""}>Completed</option><option value="rejected" ${x.status==="rejected"?"selected":""}>Rejected</option></select>`:""}<button data-idea-delete="${x.id}" class="danger-text">Delete</button></footer></article>`).join("")||projectEmpty("💡","No ideas yet","Give the team somewhere to explore options before committing.")}</div>`;
}

function projectDecisionsMarkup(){
  const items=state.projectWorkspace?.decisions||[];const can=state.activeProject?.permissions?.manageDecisions;
  return `<div class="project-section-head"><div><h2>Decision Log</h2><p>Record what was chosen and why so nobody has to reconstruct the reasoning six months later.</p></div></div>
    ${can?`<form id="project-decision-form" class="project-inline-form"><label>Decision title<input id="decision-title" placeholder="Use RAID 10 for application volume"></label><label>Decision<textarea id="decision-text" placeholder="What the team decided"></textarea></label><label>Rationale<textarea id="decision-rationale" placeholder="Why this option won and what alternatives were considered"></textarea></label><button class="primary-action">Record decision</button></form>`:""}
    <div class="decision-timeline">${items.map((x,i)=>`<article class="decision-card"><div class="decision-number">#${items.length-i}</div><div><h3>${esc(x.title)}</h3><div class="decision-answer">${markdown(x.decision)}</div>${x.rationale?`<div class="decision-rationale"><b>Why</b>${markdown(x.rationale)}</div>`:""}<footer>${esc(x.createdByEmail)} · ${formatWhen(x.createdAt)} ${can?`<button data-decision-delete="${x.id}" class="danger-text">Delete</button>`:""}</footer></div></article>`).join("")||projectEmpty("◆","No decisions recorded","Approved architecture and process choices belong here.")}</div>`;
}

function projectRisksMarkup(){
  const items=state.projectWorkspace?.risks||[];
  return `<div class="project-section-head"><div><h2>Risk Register</h2><p>Track technical, vendor, scheduling, compatibility, and rollback risks.</p></div></div>
    <form id="project-risk-form" class="project-inline-form form-grid-3"><label>Risk<input id="risk-title" placeholder="Vendor has not confirmed Server 2025 support"></label><label>Impact<select id="risk-impact"><option>low</option><option selected>medium</option><option>high</option></select></label><label>Likelihood<select id="risk-likelihood"><option>low</option><option selected>medium</option><option>high</option></select></label><label class="span-3">Mitigation<input id="risk-mitigation" placeholder="Verification or rollback step"></label><button class="primary-action">Add risk</button></form>
    <div class="risk-grid">${items.map(x=>`<article class="risk-card ${x.status==="closed"?"closed":""}"><header><span class="risk-impact impact-${esc(x.impact)}">${esc(x.impact)} impact</span><span>${esc(x.likelihood)} likelihood</span></header><h3>${esc(x.title)}</h3><p>${esc(x.mitigation||"No mitigation documented yet.")}</p><footer><span>${esc(x.status)}</span><div>${x.status!=="closed"?`<button data-risk-close="${x.id}">Close</button>`:""}<button data-risk-delete="${x.id}" class="danger-text">Delete</button></div></footer></article>`).join("")||projectEmpty("⚠","No risks documented","Document uncertainties before they surprise the team.")}</div>`;
}

function projectActivityMarkup(){
  const items=state.projectWorkspace?.activity||[];
  return `<div class="project-section-head"><div><h2>Activity</h2><p>Shared audit trail of important project changes.</p></div></div><div class="activity-timeline">${items.map(x=>`<article><div class="activity-dot"></div><div><b>${esc(x.actorEmail)}</b><span>${esc(x.action)}</span><small>${formatWhen(x.createdAt)}</small></div></article>`).join("")||projectEmpty("🕘","No activity yet","Project changes will show up here.")}</div>`;
}

function projectMembersMarkup(){
  const p=state.activeProject;const members=p?.members||[];const perms=p?.permissions||{};
  return `<div class="project-section-head"><div><h2>Members & Roles</h2><p>Owners control roles and deletion. Admins manage the workspace and regular members. Members collaborate without changing workspace structure.</p></div></div>
    ${perms.manageMembers?`<form id="project-invite-form" class="project-inline-form form-grid-3"><label>Email<input id="project-invite-email" type="email" placeholder="user@${esc(state.config?.allowedDomain||"your-domain.example")}" required></label><label>Role<select id="project-invite-role" ${perms.manageRoles?"":"disabled"}><option value="member">Member</option>${perms.manageRoles?'<option value="admin">Admin</option><option value="owner">Owner</option>':""}</select></label><div class="invite-help">Access appears automatically when that user signs in.</div><button class="primary-action">Invite</button></form>`:""}
    <div class="member-table">${members.map(x=>`<article class="member-row"><div class="project-member-avatar">${esc(initials(x.email))}</div><div><b>${esc(x.email)}</b><span>${x.primaryOwner?"Primary Owner":roleLabel(x.role)}</span></div><div>${perms.manageRoles&&!x.primaryOwner?`<select data-member-role="${x.id}" data-member-email="${esc(x.email)}"><option value="member" ${x.role==="member"?"selected":""}>Member</option><option value="admin" ${x.role==="admin"?"selected":""}>Admin</option><option value="owner" ${x.role==="owner"?"selected":""}>Owner</option></select>`:`<span class="role-chip role-${esc(x.role)}">${roleLabel(x.role)}</span>`}</div><div>${!x.primaryOwner&&((perms.manageMembers)||(x.email||"").toLowerCase()===(state.account.email||"").toLowerCase())?`<button data-remove-member="${x.id}" data-member-email="${esc(x.email)}" class="danger-text">${(x.email||"").toLowerCase()===(state.account.email||"").toLowerCase()?"Leave":"Remove"}</button>`:""}</div></article>`).join("")}</div>
    ${perms.deleteProject?`<section class="project-danger"><div><b>Delete project</b><span>Permanently removes the workspace, project chat, notes, tasks, ideas, links and stored files.</span></div><button id="project-delete">Delete project</button></section>`:""}`;
}

function projectTabMarkup(){
  return ({overview:projectOverviewMarkup,beepy:projectBeepyMarkup,chat:projectChatMarkup,notes:projectNotesMarkup,files:projectFilesMarkup,links:projectLinksMarkup,tasks:projectTasksMarkup,ideas:projectIdeasMarkup,decisions:projectDecisionsMarkup,risks:projectRisksMarkup,activity:projectActivityMarkup,members:projectMembersMarkup}[state.projectTab]||projectOverviewMarkup)();
}

function projectToolboxMarkup(){
  if(!state.projectToolboxOpen||!state.activeProject?.permissions?.manageWorkspace)return "";
  const p=state.activeProject,w=state.projectWorkspace||{},layout=state.projectLayoutDraft||w.settings?.layout||[];
  return `<div class="project-modal-backdrop"><form id="project-toolbox-form" class="project-toolbox"><header><div><span>PROJECT TOOLBOX</span><h2>Customize ${esc(p.name)}</h2><p>Owners and Admins control the shared workspace layout and project settings.</p></div><button id="project-toolbox-close" type="button">×</button></header><div class="toolbox-grid"><section><h3>Project details</h3><label>Name<input id="toolbox-project-name" value="${esc(p.name)}"></label><label>Description<textarea id="toolbox-project-description">${esc(p.description||"")}</textarea></label><label>Client<input id="toolbox-client-name" value="${esc(w.settings?.clientName||"")}" placeholder="Optional client / company"></label><label>Status<select id="toolbox-project-status">${["planning","active","waiting","blocked","completed","archived"].map(x=>`<option value="${x}" ${w.settings?.status===x?"selected":""}>${x}</option>`).join("")}</select></label></section><section><h3>Overview layout</h3><p class="toolbox-help">Choose which cards appear on Overview and move them into the order your team wants.</p><div class="layout-editor">${PROJECT_WIDGETS.map(([id,label])=>{const enabled=layout.includes(id),index=layout.indexOf(id);return `<div class="layout-row ${enabled?"enabled":"disabled"}"><label><input type="checkbox" data-layout-toggle="${id}" ${enabled?"checked":""}> ${esc(label)}</label><div><button type="button" data-layout-up="${id}" ${!enabled||index<=0?"disabled":""}>↑</button><button type="button" data-layout-down="${id}" ${!enabled||index===layout.length-1?"disabled":""}>↓</button></div></div>`}).join("")}</div></section></div><footer><span>Changes are shared with everyone in this project.</span><div><button id="project-toolbox-cancel" type="button" class="secondary-action">Cancel</button><button class="primary-action">Save workspace</button></div></footer></form></div>`;
}

function projectLinkPreviewMarkup(){
  const id=state.projectLinkPreview;if(!id)return "";const link=(state.projectWorkspace?.links||[]).find(x=>x.id===id);if(!link)return "";
  return `<div class="project-modal-backdrop"><div class="link-preview-modal"><header><div><h3>${esc(link.title)}</h3><span>${esc(link.url)}</span></div><button id="link-preview-close">×</button></header><iframe src="${esc(link.url)}" sandbox="allow-forms allow-scripts allow-same-origin allow-popups" referrerpolicy="no-referrer"></iframe><footer><span>Some sites block embedding with browser security headers.</span><a href="${esc(link.url)}" target="_blank" rel="noopener">Open in new tab ↗</a></footer></div></div>`;
}

function projectWorkspaceMarkup(){
  const p=state.activeProject;if(!p)return "";const w=state.projectWorkspace||{};
  const tabs=[["overview","▦","Overview"],["beepy","✦","Beepy"],["chat","💬","Team Chat"],["notes","📝","Notes"],["files","📁","Files"],["links","🔗","Links"],["tasks","✓","Tasks"],["ideas","💡","Ideas"],["decisions","◆","Decisions"],["risks","⚠","Risks"],["activity","◷","Activity"],["members","👥","Members"]];
  return `<section class="project-workspace-v3"><div class="project-workspace-header"><div><button id="projects-back" class="back-link">← Projects</button><div class="project-title-line"><h1>${esc(p.name)}</h1><span class="role-chip role-${esc(p.role)}">${roleLabel(p.role)}</span><span class="project-status status-${esc(w.settings?.status||"active")}">${esc(w.settings?.status||"active")}</span></div><p>${esc(p.description||"Shared Beepy project workspace")}</p></div><div class="project-header-actions"><div class="member-stack">${(p.members||[]).slice(0,5).map(x=>`<span title="${esc(x.email)}">${esc(initials(x.email))}</span>`).join("")}</div><button id="project-refresh" class="secondary-action">↻ Refresh</button>${p.permissions?.manageWorkspace?'<button id="project-toolbox" class="primary-action">🧰 Toolbox</button>':""}</div></div><div class="project-shell"><aside class="project-nav">${tabs.map(([id,icon,label])=>`<button data-project-tab="${id}" class="${state.projectTab===id?"active":""}"><span>${icon}</span>${label}${id==="chat"&&w.teamMessages?.length?`<em>${w.teamMessages.length}</em>`:""}</button>`).join("")}</aside><main class="project-content">${projectTabMarkup()}</main></div>${projectToolboxMarkup()}${projectLinkPreviewMarkup()}</section>`;
}

function projectsMarkup(){
  if(state.activeProject)return projectWorkspaceMarkup();
  const cards=state.projects.map(project=>`<button class="project-card" data-project="${esc(project.id)}"><div class="project-card-icon">▣</div><div class="project-card-copy"><h3>${esc(project.name)}</h3><p>${esc(project.description||"No description")}</p></div><div class="project-card-meta"><span>${Number(project.memberCount||1)} people</span><span class="role-chip role-${esc(project.role)}">${roleLabel(project.role)}</span></div></button>`).join("");
  return `<section class="projects-page"><div class="projects-title-row"><div><h1>Projects</h1><p>Shared MSP workspaces for planning, discussion, files, decisions, risks, tasks and project-aware Beepy.</p></div><button id="project-new" class="primary-action">＋ New project</button></div>${state.projectCreateOpen?`<form id="project-create-form" class="project-create-card"><div><h3>Create project</h3><p>Start a technical workspace and invite the team after creation.</p></div><label>Project name<input id="project-name" maxlength="200" placeholder="Example: Stroudsburg Server Upgrade" required></label><label>Description<textarea id="project-description" maxlength="4000" placeholder="Goal, scope, client, expected outcome…"></textarea></label><div class="project-form-actions"><button id="project-create-cancel" type="button" class="secondary-action">Cancel</button><button id="project-create-submit" type="submit" class="primary-action">Create project</button></div></form>`:""}<div class="project-grid">${cards||`<div class="projects-empty"><div>▣</div><h3>No projects yet</h3><p>Create your first project workspace and bring the MSP team into one place.</p></div>`}</div></section>`;
}

function bindProjectEvents(){
  document.getElementById("project-new")?.addEventListener("click",()=>{state.projectCreateOpen=true;render()});
  document.getElementById("project-create-cancel")?.addEventListener("click",()=>{state.projectCreateOpen=false;render()});
  document.getElementById("project-create-form")?.addEventListener("submit",createProject);
  document.querySelectorAll("[data-project]").forEach(x=>x.addEventListener("click",()=>openProject(x.dataset.project)));
  document.getElementById("projects-back")?.addEventListener("click",async()=>{state.activeProject=null;state.projectWorkspace=null;await refreshProjects();render()});
  document.getElementById("project-refresh")?.addEventListener("click",()=>reloadActiveProject());
  document.querySelectorAll("[data-project-tab]").forEach(x=>x.addEventListener("click",()=>projectNavigate(x.dataset.projectTab)));
  document.getElementById("project-toolbox")?.addEventListener("click",openProjectToolbox);
  document.getElementById("project-toolbox-close")?.addEventListener("click",()=>{state.projectToolboxOpen=false;render()});
  document.getElementById("project-toolbox-cancel")?.addEventListener("click",()=>{state.projectToolboxOpen=false;render()});
  document.getElementById("project-toolbox-form")?.addEventListener("submit",saveProjectToolbox);
  document.querySelectorAll("[data-layout-toggle]").forEach(x=>x.addEventListener("change",()=>toggleLayoutWidget(x.dataset.layoutToggle,x.checked)));
  document.querySelectorAll("[data-layout-up]").forEach(x=>x.addEventListener("click",()=>moveLayoutWidget(x.dataset.layoutUp,-1)));
  document.querySelectorAll("[data-layout-down]").forEach(x=>x.addEventListener("click",()=>moveLayoutWidget(x.dataset.layoutDown,1)));
  document.getElementById("project-chat-form")?.addEventListener("submit",postProjectMessage);
  document.getElementById("project-beepy-form")?.addEventListener("submit",askProjectBeepy);
  document.getElementById("project-beepy-quick-form")?.addEventListener("submit",askProjectBeepy);
  document.querySelectorAll("[data-project-message]").forEach(x=>x.addEventListener("contextmenu",e=>showProjectMessageMenu(e,x.dataset.projectMessage)));
  document.getElementById("project-note-form")?.addEventListener("submit",createProjectNote);
  document.querySelectorAll("[data-note-pin]").forEach(x=>x.addEventListener("click",()=>toggleProjectNote(x.dataset.notePin,x.dataset.pinned!=="1")));
  document.querySelectorAll("[data-note-delete]").forEach(x=>x.addEventListener("click",()=>deleteProjectNote(x.dataset.noteDelete)));
  document.getElementById("project-file-form")?.addEventListener("submit",uploadProjectFile);
  document.querySelectorAll("[data-file-download]").forEach(x=>x.addEventListener("click",()=>downloadProjectFile(x.dataset.fileDownload,x.dataset.filename)));
  document.querySelectorAll("[data-file-delete]").forEach(x=>x.addEventListener("click",()=>deleteProjectFile(x.dataset.fileDelete)));
  document.getElementById("project-link-form")?.addEventListener("submit",createProjectLink);
  document.querySelectorAll("[data-link-preview]").forEach(x=>x.addEventListener("click",()=>{state.projectLinkPreview=x.dataset.linkPreview;render()}));
  document.querySelectorAll("[data-link-delete]").forEach(x=>x.addEventListener("click",()=>deleteProjectLink(x.dataset.linkDelete)));
  document.getElementById("link-preview-close")?.addEventListener("click",()=>{state.projectLinkPreview=null;render()});
  document.getElementById("project-task-form")?.addEventListener("submit",createProjectTask);
  document.querySelectorAll("[data-task-status]").forEach(x=>x.addEventListener("change",()=>updateProjectTask(x.dataset.taskStatus,x.value)));
  document.querySelectorAll("[data-task-delete]").forEach(x=>x.addEventListener("click",()=>deleteProjectTask(x.dataset.taskDelete)));
  document.getElementById("project-idea-form")?.addEventListener("submit",createProjectIdea);
  document.querySelectorAll("[data-idea-vote]").forEach(x=>x.addEventListener("click",()=>voteProjectIdea(x.dataset.ideaVote,Number(x.dataset.vote))));
  document.querySelectorAll("[data-idea-status]").forEach(x=>x.addEventListener("change",()=>setProjectIdeaStatus(x.dataset.ideaStatus,x.value)));
  document.querySelectorAll("[data-idea-delete]").forEach(x=>x.addEventListener("click",()=>deleteProjectIdea(x.dataset.ideaDelete)));
  document.getElementById("project-decision-form")?.addEventListener("submit",createProjectDecision);
  document.querySelectorAll("[data-decision-delete]").forEach(x=>x.addEventListener("click",()=>deleteProjectDecision(x.dataset.decisionDelete)));
  document.getElementById("project-risk-form")?.addEventListener("submit",createProjectRisk);
  document.querySelectorAll("[data-risk-close]").forEach(x=>x.addEventListener("click",()=>closeProjectRisk(x.dataset.riskClose)));
  document.querySelectorAll("[data-risk-delete]").forEach(x=>x.addEventListener("click",()=>deleteProjectRisk(x.dataset.riskDelete)));
  document.getElementById("project-invite-form")?.addEventListener("submit",inviteProjectMember);
  document.querySelectorAll("[data-member-role]").forEach(x=>x.addEventListener("change",()=>changeProjectMemberRole(x.dataset.memberRole,x.value,x.dataset.memberEmail)));
  document.querySelectorAll("[data-remove-member]").forEach(x=>x.addEventListener("click",()=>removeProjectMember(x.dataset.removeMember,x.dataset.memberEmail)));
  document.getElementById("project-delete")?.addEventListener("click",deleteActiveProject);
}


function render(){
  closeConversationMenu();

  // Preserve active work whenever a deliberate UI render is necessary.
  const oldQuestion=document.getElementById("question");
  if(oldQuestion)state.draft=oldQuestion.value;
  const draft=state.draft||"";
  const selectionStart=oldQuestion?.selectionStart??draft.length;
  const selectionEnd=oldQuestion?.selectionEnd??draft.length;
  const questionHadFocus=document.activeElement===oldQuestion;

  const oldChat=document.querySelector(".chat");
  const oldScrollTop=oldChat?.scrollTop??0;
  const wasNearBottom=!oldChat||oldChat.scrollHeight-oldChat.scrollTop-oldChat.clientHeight<120;

  const oldHistory=document.querySelector(".history");
  const oldHistoryScroll=oldHistory?.scrollTop??0;
  const oldWorkspacePage=document.querySelector(".workspace-page");
  const oldWorkspaceScroll=oldWorkspacePage?.scrollTop??0;
  const oldProjectChat=document.querySelector(".project-chat-stream");
  const oldProjectChatTop=oldProjectChat?.scrollTop??0;
  const oldProjectChatNearBottom=!oldProjectChat||oldProjectChat.scrollHeight-oldProjectChat.scrollTop-oldProjectChat.clientHeight<90;
  const oldProjectBeepy=document.querySelector(".project-beepy-stream");
  const oldProjectBeepyTop=oldProjectBeepy?.scrollTop??0;
  const oldProjectBeepyNearBottom=!oldProjectBeepy||oldProjectBeepy.scrollHeight-oldProjectBeepy.scrollTop-oldProjectBeepy.clientHeight<90;

  if(!state.account){renderLogin();return}

  const messages=state.messages.map(messageMarkup).join("");
  const welcome=!state.messages.length?`<div class="welcome"><h1>What can Beepy help with?</h1><p>Search Autotask tickets, troubleshoot with Odysseus RAG, or search approved Microsoft 365 email history.</p><div class="quick"><button data-prompt="Show me recent VPN tickets">Recent VPN tickets</button><button data-prompt="Why would NetExtender authenticate but not receive an IP?">VPN troubleshooting</button>${state.emailStatus?.authorized?'<button data-prompt="Find emails about a server replacement quote">Search tenant email</button>':""}</div></div>`:"";
  const workspaceBody=state.page==="projects"
    ? `<section class="workspace-page">${projectsMarkup()}</section>`
    : `<section class="chat">${welcome}<div class="messages">${messages}${state.busy?'<article class="message assistant typing-message"><div class="avatar">B</div><div class="bubble typing">● ● ●</div></article>':""}<div id="end"></div></div></section>
      <section class="composer"><div class="status">${state.status?`${Number(state.status.tickets).toLocaleString()} tickets · ${Number(state.status.notes).toLocaleString()} notes`:"Index loading…"}</div>
        <textarea id="question" placeholder="Ask about a ticket, technical issue, project, or indexed Microsoft 365 email…"></textarea>
        <div class="composer-row"><select id="mode" aria-label="Beepy mode"><option value="auto">Auto</option><option value="tickets">Tickets</option><option value="tech">Tech Chat</option><option value="email">Email Intelligence${state.emailStatus?.authorized?"":" 🔒"}</option></select><button id="send" class="send">➤</button></div></section>`;

  app.innerHTML=`<div class="app ${state.collapsed?"collapsed":""} ${state.preferences.showDetails?"":"hide-details"}">
    <aside class="sidebar">
      <div class="sidebar-tools"><button id="settings" class="sidebar-tool" title="Settings" aria-label="Settings">⚙</button><button id="collapse" class="sidebar-tool" title="Toggle sidebar" aria-label="Toggle sidebar">☰</button></div>
      <div class="brand">BEEPY<span>BUSINESS INTELLIGENCE</span></div>
      <button id="home" class="nav ${state.page==="chat"?"active":""}">⌂ <span>Home</span></button>
      <button id="new" class="nav">＋ <span>New chat</span></button>
      <button id="projects" class="nav ${state.page==="projects"?"active":""}">▣ <span>Projects</span></button>
      <div class="history-head"><span>Conversations</span><button id="new2">＋</button></div>
      <div class="history">${historyMarkup()}</div>
      <div class="sidebar-hint">Right-click a chat to delete it</div>
    </aside>
    <main class="workspace"><header><div><h2>${state.page==="projects"?"Beepy Projects":"Beepy"}</h2><p>${state.page==="projects"?"Shared business project workspaces":"Autotask intelligence and Odysseus technical support"}</p></div><div class="user"><b>${esc(state.account.name)}</b><span>${esc(state.account.email)}</span></div></header>
      ${workspaceBody}
    </main>
    ${settingsMarkup()}
  </div>`;

  document.getElementById("collapse").onclick=()=>{
    state.collapsed=!state.collapsed;
    localStorage.setItem("mbc_beepy_sidebar_collapsed",String(state.collapsed));
    render();
  };
  document.getElementById("home").onclick=()=>{state.page="chat";state.activeProject=null;state.projectWorkspace=null;render()};
  document.getElementById("new").onclick=()=>{state.page="chat";newConversation()};
  document.getElementById("new2").onclick=()=>{state.page="chat";newConversation()};
  document.getElementById("projects").onclick=showProjects;
  document.getElementById("send")?.addEventListener("click",send);
  const modeSelect=document.getElementById("mode");
  if(modeSelect){modeSelect.value=state.mode;modeSelect.onchange=event=>state.mode=event.target.value}
  const questionBox=document.getElementById("question");
  if(questionBox){
    questionBox.oninput=()=>{state.draft=questionBox.value};
    questionBox.onkeydown=event=>{
      if(state.preferences.enterToSend&&event.key==="Enter"&&!event.shiftKey){
        event.preventDefault();
        send();
      }
    };
  }
  bindHistoryEvents();
  bindSettingsEvents();
  bindProjectEvents();
  document.querySelectorAll("[data-prompt]").forEach(button=>button.onclick=()=>{
    const question=document.getElementById("question");
    if(!question)return;
    question.value=button.dataset.prompt;
    state.draft=question.value;
    question.focus();
  });

  const newQuestion=document.getElementById("question");
  if(newQuestion){
    newQuestion.value=draft;
    if(questionHadFocus){
      newQuestion.focus({preventScroll:true});
      try{newQuestion.setSelectionRange(selectionStart,selectionEnd)}catch{}
    }
  }

  const newHistory=document.querySelector(".history");
  if(newHistory)newHistory.scrollTop=oldHistoryScroll;

  const newWorkspacePage=document.querySelector(".workspace-page");
  if(newWorkspacePage)newWorkspacePage.scrollTop=oldWorkspaceScroll;
  const newProjectChat=document.querySelector(".project-chat-stream");
  if(newProjectChat)newProjectChat.scrollTop=oldProjectChatNearBottom?newProjectChat.scrollHeight:oldProjectChatTop;
  const newProjectBeepy=document.querySelector(".project-beepy-stream");
  if(newProjectBeepy)newProjectBeepy.scrollTop=oldProjectBeepyNearBottom?newProjectBeepy.scrollHeight:oldProjectBeepyTop;

  const newChat=document.querySelector(".chat");
  if(newChat){
    if(state.preferences.autoScroll&&wasNearBottom)newChat.scrollTop=newChat.scrollHeight;
    else newChat.scrollTop=oldScrollTop;
  }
}

window.addEventListener("contextmenu",event=>{
  if(!event.target.closest?.("[data-conv]"))closeConversationMenu();
  if(!event.target.closest?.("[data-project-message]"))closeProjectMessageMenu();
});
window.addEventListener("resize",()=>{closeConversationMenu();closeProjectMessageMenu()});
window.addEventListener("keydown",event=>{
  if(event.key==="Escape"){
    closeConversationMenu();
    closeProjectMessageMenu();
    if(state.projectLinkPreview){state.projectLinkPreview=null;render();return}
    if(state.projectToolboxOpen){state.projectToolboxOpen=false;render();return}
    if(state.settingsOpen){state.settingsOpen=false;render()}
  }
});

(async()=>{
  try{
    await loadConfig();
    await handleCallback();
    await activate();
    if(state.token)await loadInitialData();
    render();
    // Only the small ticket/note counter refreshes in the background.
    // Chats, the composer, and the conversation view are never rebuilt here.
    if(state.token){
      setInterval(refreshStatus,60000);
      setInterval(async()=>{
        if(state.page!=="projects"||!state.activeProject||state.projectBusy)return;
        const focused=document.activeElement;
        if(focused&&["INPUT","TEXTAREA","SELECT"].includes(focused.tagName))return;
        if(["notes","files","links","tasks","ideas","decisions","risks","members"].includes(state.projectTab))return;
        const activeDraft=document.querySelector("#project-chat-input,#project-beepy-input,#project-beepy-quick");
        if(activeDraft&&activeDraft.value.trim())return;
        try{await refreshProjectWorkspace(true)}catch(error){console.warn("Project refresh failed:",error)}
      },15000);
    }
  }catch(error){
    app.innerHTML=`<div class="loading error">${esc(error.message)}</div>`;
  }
})();
