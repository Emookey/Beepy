const app = document.getElementById("app");
const state = {
  config:null, account:null, token:null, conversations:[], conversationId:null,
  messages:[], mode:"smart", busy:false, status:null, collapsed:false
};

const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
}[c]));

function base64url(bytes){
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");
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
  if(options.body)headers["Content-Type"]="application/json";
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
async function refresh(){
  if(!state.token)return;
  [state.conversations,state.status]=await Promise.all([
    api("/api/conversations"),api("/api/status")
  ]);
  render();
}
async function openConversation(id){
  const data=await api(`/api/conversations/${id}`);
  state.conversationId=id;state.messages=data.messages;render();
}
function newConversation(){state.conversationId=null;state.messages=[];render()}
async function send(){
  const box=document.getElementById("question");
  const q=(box?.value||"").trim();
  if(!q||state.busy)return;
  box.value="";
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
        render();
      }
    }
    state.conversations=await api("/api/conversations");
  }catch(error){
    state.messages[assistantIndex].content=`**Error:** ${error.message}`;
  }finally{
    state.busy=false;
    render();
  }
}
function renderLogin(){
  app.innerHTML=`<main class="login-page"><section class="login-card">
    
    <h1>Sign in to MBC Intelligence</h1>
    <p>Private access through Microsoft Entra ID and Tailscale.</p>
    <button id="login" class="microsoft-button"><span class="ms-logo"><i></i><i></i><i></i><i></i></span>Sign in with Microsoft</button>
  </section></main>`;
  document.getElementById("login").onclick=beginLogin;
}
function render(){
  if(!state.account){renderLogin();return}
  const history=state.conversations.map(c=>`<button class="${c.id===state.conversationId?"selected":""}" data-conv="${c.id}" title="${esc(c.title)}">${esc(c.title)}</button>`).join("");
  const messages=state.messages.map(m=>`<article class="message ${m.role}">
    <div class="avatar">${m.role==="assistant"?"B":esc((state.account.name||"U")[0])}</div>
    <div class="bubble">${m.role==="assistant"?markdown(m.content):esc(m.content)}
    ${m.sources?.length?`<div class="sources">Sources: ${m.sources.map(s=>s.url?`<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.ticketNumber||s.title)}</a>`:esc(s.ticketNumber||s.title)).filter(Boolean).join(", ")}</div>`:""}
    ${m.engine?`<div class="engine">${esc(m.engine)}${m.elapsedMs?` · ${(m.elapsedMs/1000).toFixed(1)}s`:""}</div>`:""}</div></article>`).join("");
  const welcome=!state.messages.length?`<div class="welcome"><h1>What can Beepy help with?</h1><p>Search synchronized Autotask tickets or ask a technical question.</p><div class="quick"><button data-prompt="Show me recent VPN tickets">Recent VPN tickets</button><button data-prompt="Why would NetExtender authenticate but not receive an IP?">VPN troubleshooting</button></div></div>`:"";
  app.innerHTML=`<div class="app ${state.collapsed?"collapsed":""}">
    <aside class="sidebar"><button id="collapse" class="collapse">☰</button><div class="brand">MBC<span>INTELLIGENCE</span></div>
      <button id="home" class="nav active">⌂ <span>Home</span></button>
      <button id="new" class="nav">＋ <span>New chat</span></button>
      <div class="history-head"><span>Conversations</span><button id="new2">＋</button></div><div class="history">${history}</div>
      <button id="logout" class="nav signout">⇥ <span>Sign out</span></button></aside>
    <main class="workspace"><header><div><h2>MBC - Beepy</h2><p>Autotask intelligence and technical support</p></div><div class="user"><b>${esc(state.account.name)}</b><span>${esc(state.account.email)}</span></div></header>
      <section class="chat">${welcome}<div class="messages">${messages}${state.busy?'<article class="message assistant"><div class="avatar">B</div><div class="bubble typing">● ● ●</div></article>':""}<div id="end"></div></div></section>
      <section class="composer"><div class="status">${state.status?`${Number(state.status.tickets).toLocaleString()} tickets · ${Number(state.status.notes).toLocaleString()} notes`:"Index loading…"}</div>
        <textarea id="question" placeholder="Ask about a client, ticket, technician, date, resolution, or technical issue…"></textarea>
        <div class="composer-row"><select id="mode"><option value="smart">Smart: tickets first</option><option value="tickets">Ticket Search</option><option value="tech">Tech Chat</option></select><button id="send" class="send">➤</button></div></section>
    </main></div>`;
  document.getElementById("collapse").onclick=()=>{state.collapsed=!state.collapsed;render()};
  document.getElementById("home").onclick=newConversation;
  document.getElementById("new").onclick=newConversation;
  document.getElementById("new2").onclick=newConversation;
  document.getElementById("logout").onclick=logout;
  document.getElementById("send").onclick=send;
  document.getElementById("mode").value=state.mode;
  document.getElementById("mode").onchange=e=>state.mode=e.target.value;
  document.getElementById("question").onkeydown=e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send()}};
  document.querySelectorAll("[data-conv]").forEach(b=>b.onclick=()=>openConversation(b.dataset.conv));
  document.querySelectorAll("[data-prompt]").forEach(b=>b.onclick=()=>{document.getElementById("question").value=b.dataset.prompt;document.getElementById("question").focus()});
  document.getElementById("end")?.scrollIntoView();
}
(async()=>{
  try{
    await loadConfig();await handleCallback();await activate();render();
    if(state.token){await refresh();setInterval(refresh,30000)}
  }catch(error){app.innerHTML=`<div class="loading error">${esc(error.message)}</div>`}
})();
