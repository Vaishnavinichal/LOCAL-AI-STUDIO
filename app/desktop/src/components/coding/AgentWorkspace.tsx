import { useCallback, useEffect, useRef, useState } from "react";
import { Brain, Check, X, Pause, Play, Search, ScanSearch, RefreshCw, Maximize2, Minimize2, ChevronDown, Circle, CheckCircle2, AlertCircle, Send, Pencil, Square, Trash2 } from "lucide-react";
import { DiffEditor } from "@monaco-editor/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { agentApi, type AgentSession, type AgentTask, type AgentChangeSet, type AgentFileChange, type ProjectIntelligenceStatus, type ManagedRuntimeStatus, type AgentPlan } from "../../services/api";
import "./agent.css";

type Props = { onClose: () => void; onError: (message: string) => void; theme?: "dark" | "light"; embedded?: boolean; initialMode?: "ask" | "planning"; unsavedPaths?: string[]; onFilesApplied?: () => void | Promise<void>; onAgentLogs?: (events: Array<{id:string;event_type:string;message:string;created_at:string}>) => void; onBusyChange?: (busy: boolean) => void; onSessionChange?: (id: string) => void; onTasksChange?: (tasks: AgentTask[]) => void; onProposeCommand?: (command: string, sessionId: string) => void; onSessionControl?: (action: "pause" | "resume" | "cancel", sessionId: string) => void; onToggleFocus?: () => void; focused?: boolean; running?: boolean; focusSessionId?: string; focusChangesetId?: string; debugStage?: string; debugDiagnosis?: {root_cause?:string;confidence?:number;alternatives?:string[];evidence?:Array<{path:string;line_start:number;line_end:number;excerpt:string}>}|null; };

const sessionTimeLabel=(value:string)=>new Intl.DateTimeFormat(undefined,{month:"short",day:"2-digit",hour:"2-digit",minute:"2-digit"}).format(new Date(value));

export default function AgentWorkspace({ onClose, onError, theme = "dark", embedded = false, initialMode, unsavedPaths = [], onFilesApplied, onAgentLogs, onBusyChange, onSessionChange, onTasksChange, onProposeCommand, onSessionControl, onToggleFocus, focused = false, running = false, focusSessionId, focusChangesetId, debugStage, debugDiagnosis }: Props) {
  const editorTheme = theme === "light" ? "vs" : "vs-dark";
  const [tab,setTab]=useState<"agent"|"project">("agent");
  const [models,setModels]=useState<Array<{id:string;name:string;runtime:string;status?:string;source_type?:string;endpoint_url?:string}>>([]);
  const [planner,setPlanner]=useState("");
  const [roles,setRoles]=useState<Record<string,string>>({});
  const [sessions,setSessions]=useState<AgentSession[]>([]);
  const [sessionId,setSessionId]=useState("");
  const [sessionDropdownOpen,setSessionDropdownOpen]=useState(false);
  const [mode,setMode]=useState<"ask"|"planning">("ask");
  const [requirement,setRequirement]=useState("");
  const [answer,setAnswer]=useState("");
  const [answerEvidence,setAnswerEvidence]=useState<Array<{path:string;line_start:number;line_end:number;excerpt:string}>>([]);
  const [changesets,setChangesets]=useState<AgentChangeSet[]>([]);
  const [activeChangesetId,setActiveChangesetId]=useState("");
  const [activeChangeId,setActiveChangeId]=useState("");
  const [plan,setPlan]=useState<AgentPlan|null>(null);
  const [tasks,setTasks]=useState<AgentTask[]>([]);
  const [planStatus,setPlanStatus]=useState("");
  const [events,setEvents]=useState<Array<{id:string;event_type:string;message:string;created_at:string}>>([]);
  const [history,setHistory]=useState<Array<{id:string;role:"user"|"assistant";content:string;created_at:string}>>([]);
  const [busy,setBusy]=useState(false);
  const [status,setStatus]=useState<ProjectIntelligenceStatus>({status:"not_indexed",files_indexed:0,indexed_at:null});
  const [scanJobId,setScanJobId]=useState("");
  const [scanProgress,setScanProgress]=useState<{completed:number;total:number|null;current_file:string}>({completed:0,total:null,current_file:""});
  const [summary,setSummary]=useState<Record<string,unknown>|null>(null);
  const [dependencies,setDependencies]=useState<Array<{source_path:string;target:string;kind:string;resolution:string}>>([]);
  const [query,setQuery]=useState("");
  const [searchMode,setSearchMode]=useState<"keyword"|"semantic"|"hybrid">("keyword");
  const [results,setResults]=useState<Array<{type:string;path:string;name?:string;line_start?:number;excerpt:string;score:number}>>([]);
  const [exclusions,setExclusions]=useState("");
  const [agentCommand,setAgentCommand]=useState("");
  const [activityError,setActivityError]=useState("");
  const [generationFailed,setGenerationFailed]=useState(false);
  const [runtimeStates,setRuntimeStates]=useState<Record<"chat"|"coding",ManagedRuntimeStatus|null>>({chat:null,coding:null});
  const [runtimeBusy,setRuntimeBusy]=useState<"chat"|"coding"|"">("");
  const [runtimeError,setRuntimeError]=useState("");
  const [chatError,setChatError]=useState("");
  const [pendingPrompt,setPendingPrompt]=useState("");
  const generatingChangesRef=useRef(false);
  const chatRequestRef=useRef<AbortController|null>(null);
  const conversationRef=useRef<HTMLDivElement>(null);
  const stickConversationToBottom=useRef(true);

  const refreshSessions=useCallback(async()=>{
    try {
      const [list,role]=await Promise.all([agentApi.sessions(),agentApi.modelRoles()]);
      setSessions(list);setModels(role.models);
      try{setRuntimeStates(await agentApi.managedRuntimes());}catch{setRuntimeStates({chat:null,coding:null});}
      const availableIds=new Set(role.models.map(item=>item.id));
      const chatEndpoint=role.models.find(item=>item.source_type==="endpoint"&&item.endpoint_url?.endsWith(":8081"));
      const codingEndpoint=role.models.find(item=>item.source_type==="endpoint"&&item.endpoint_url?.endsWith(":8082"));
      const savedPlanner=role.roles.planner;
      const savedPlannerModel=role.models.find(item=>item.id===savedPlanner);
      const planner=(savedPlanner&&availableIds.has(savedPlanner)&&!(chatEndpoint&&savedPlannerModel?.name.toLowerCase().includes("coder")))
        ?savedPlanner:chatEndpoint?.id||role.models.find(item=>!item.name.toLowerCase().includes("coder"))?.id||role.models[0]?.id||"";
      const savedCoding=role.roles.coding;
      const nextRoles:Record<string,string>={...role.roles,planner,coding:savedCoding&&availableIds.has(savedCoding)?savedCoding:codingEndpoint?.id||role.models.find(item=>item.name.toLowerCase().includes("coder"))?.id||""};
      setPlanner(planner);setRoles(nextRoles);
      try{setStatus(await agentApi.projectStatus());}catch(error){if(!String(error).includes("Open a project in Coding Workspace first"))throw error;setStatus({status:"not_indexed",files_indexed:0,indexed_at:null});}
    }
    catch(e){ onError(String(e)); }
  },[onError]);

  useEffect(()=>{ void refreshSessions(); },[refreshSessions]);
  useEffect(()=>{ if(initialMode){setTab("agent");setMode(initialMode);} },[initialMode]);
  useEffect(()=>{ onBusyChange?.(busy); },[busy,onBusyChange]);
  useEffect(()=>{ onSessionChange?.(sessionId); },[sessionId,onSessionChange]);
  useEffect(()=>{ onTasksChange?.(tasks); },[tasks,onTasksChange]);
  useEffect(()=>{const node=conversationRef.current;if(node&&stickConversationToBottom.current)node.scrollTop=node.scrollHeight;},[history,pendingPrompt,busy,answer]);
  const selected=sessions.find(s=>s.id===sessionId);
  useEffect(()=>{
    if(!sessionId||selected?.status!=="planning")return;
    let alive=true,polling=false,timer=0,failures=0;
    const poll=async()=>{
      if(polling)return;
      polling=true;
      try{
        const sessionState=await agentApi.session(sessionId);
        if(!alive)return;
        failures=0;setActivityError("");
        setSessions(list=>list.map(item=>item.id===sessionState.id?sessionState:item));
        if(sessionState.status!=="planning"){
          const [next,planState]=await Promise.all([
            agentApi.events(sessionId),
            agentApi.getPlan(sessionId).catch(error=>{if(String(error).includes("No plan exists"))return null;throw error;})
          ]);
          if(!alive)return;
          setEvents(next);onAgentLogs?.(next);
          if(planState){setTasks(planState.tasks);setPlan(planState.plan);setPlanStatus(planState.status);}
          setChatError("");
          window.clearTimeout(timer);
        }else{
          timer=window.setTimeout(()=>void poll(),2000);
        }
      }catch(error){if(alive){const detail=String(error);setActivityError(detail);if(/\b404\b|not found/i.test(detail)){window.clearTimeout(timer);return;}failures+=1;timer=window.setTimeout(()=>void poll(),Math.min(1500*2**(failures-1),15000));}}
      finally{polling=false;}
    };
    timer=window.setTimeout(()=>void poll(),1500);
    void poll();
    return()=>{alive=false;window.clearTimeout(timer);};
  },[sessionId,selected?.status,onAgentLogs]);
  const refreshSession=async(id:string)=>{
    const [p,e,h]=await Promise.all([agentApi.getPlan(id).catch(error=>{if(String(error).includes("No plan exists"))return null;throw error;}),agentApi.events(id),agentApi.history(id)]);
    if(p){setPlan(p.plan);setTasks(p.tasks);setPlanStatus(p.status);setAgentCommand(current=>current||p.plan.verification_command||"");}
    else {setPlan(null);setTasks([]);setPlanStatus("");}
    setEvents(e);onAgentLogs?.(e);setHistory(h);setAnswer([...h].reverse().find(m=>m.role==="assistant")?.content||"");
    const failedWithoutPlan=sessions.find(item=>item.id===id)?.status==="failed"&&!p;
    let proposals:AgentChangeSet[]=[];
    if(!failedWithoutPlan)try{proposals=await agentApi.changes(id);}catch(error){setActivityError(String(error));}
    setChangesets(proposals);
    if(!proposals.some(proposal=>proposal.id===activeChangesetId))setActiveChangesetId(proposals[0]?.id||"");
    return proposals;
  };
  const create=async()=>{
    if(!planner){onError("Select a planner model in this panel first");return;}
    setBusy(true);
    try {const s=await agentApi.create(mode,planner);setSessions(old=>[s,...old.filter(x=>x.id!==s.id)]);setSessionId(s.id);setPlan(null);setTasks([]);setEvents([]);setAnswer("");setAnswerEvidence([]);setChangesets([]);setActiveChangesetId("");setAgentCommand("");}
    catch(e){onError(String(e));} finally{setBusy(false);}
  };
  const submit=async()=>{
    if(!requirement.trim())return;
    if(!sessionId){onError("Create an agent session first");return;}
    const activeMode=selected?.mode||mode;
    const prompt=requirement.trim();stickConversationToBottom.current=true;setPendingPrompt(prompt);setChatError("");setBusy(true);setAnswer("");setAnswerEvidence([]);const controller=new AbortController();chatRequestRef.current=controller;
    if(activeMode==="planning")setSessions(items=>items.map(item=>item.id===sessionId?{...item,status:"planning",error:""}:item));
    try {
      if(activeMode==="planning") {const r=await agentApi.plan(sessionId,prompt,planner,controller.signal);if(r.clarification_questions?.length){setAnswer("Please clarify:\n"+r.clarification_questions.map(q=>"• "+q).join("\n"));setPlan(null);setTasks([]);}else{setPlan(r.plan||null);setTasks(r.tasks||[]);setPlanStatus("waiting_for_approval");}setSessions(s=>s.map(x=>x.id===r.session.id?r.session:x));}
      else {const r=await agentApi.ask(sessionId,prompt,planner,controller.signal);setAnswer(r.answer);setAnswerEvidence(r.context.items);setSessions(s=>s.map(x=>x.id===r.session.id?r.session:x));}
      if(selected&&!selected.title_manually_edited&&(!selected.title||/^new session$/i.test(selected.title))){const renamed=await agentApi.renameSession(sessionId,prompt.replace(/\s+/g," ").slice(0,64));setSessions(s=>s.map(x=>x.id===renamed.id?renamed:x));}setRequirement("");await refreshSession(sessionId);
    } catch(e){if(controller.signal.aborted){setChatError("Chat stopped.");}else{const detail=String(e);setChatError(detail);onError(detail);try{const latest=await agentApi.session(sessionId);setSessions(items=>items.map(item=>item.id===latest.id?latest:item));await refreshSession(sessionId);if(["waiting_for_approval","plan_approved","ready"].includes(latest.status))setChatError("");}catch{/* Keep the original request error visible. */}}} finally{if(chatRequestRef.current===controller)chatRequestRef.current=null;setPendingPrompt("");setBusy(false);}
  };
  const stopChat=()=>chatRequestRef.current?.abort();
  const retryPlanning=async()=>{if(!selected||selected.mode!=="planning"||selected.status!=="failed"||plan||busy)return;setBusy(true);setActivityError("");setPlan(null);setTasks([]);setPlanStatus("");setSessions(items=>items.map(item=>item.id===sessionId?{...item,status:"planning",error:""}:item));try{const result=await agentApi.retryPlan(sessionId);setPlan(result.plan||null);setTasks(result.tasks||[]);setPlanStatus("waiting_for_approval");setSessions(items=>items.map(item=>item.id===result.session.id?result.session:item));await refreshSession(sessionId);}catch(error){setActivityError(String(error));onError(String(error));try{const latest=await agentApi.session(sessionId);setSessions(items=>items.map(item=>item.id===latest.id?latest:item));}catch{/* Keep the retry error visible if the session itself is unavailable. */}}finally{setBusy(false);}};
  const loadSession=async(id:string)=>{setSessionDropdownOpen(false);stickConversationToBottom.current=true;setSessionId(id);const item=sessions.find(s=>s.id===id);if(item)setMode(item.mode);setPlan(null);setTasks([]);setPlanStatus("");setAnswer("");setChatError("");setAgentCommand("");setActivityError("");setEvents([]);setHistory([]);setChangesets([]);setActiveChangesetId("");try{await refreshSession(id);}catch(e){setActivityError(String(e));onError(String(e));}};
  useEffect(()=>{if(!focusSessionId||!focusChangesetId)return;setTab("agent");void(async()=>{await loadSession(focusSessionId);try{const proposals=await agentApi.changes(focusSessionId);setChangesets(proposals);setActiveChangesetId(focusChangesetId);setActiveChangeId(proposals.find(item=>item.id===focusChangesetId)?.changes[0]?.id||"");}catch(e){onError(String(e));}})();},[focusSessionId,focusChangesetId]);
  const renameSession=async(title:string)=>{if(!selected||!title.trim())return;try{const updated=await agentApi.renameSession(selected.id,title.trim());setSessions(items=>items.map(item=>item.id===updated.id?updated:item));}catch(e){onError(String(e));}};
  const deleteSession=async()=>{if(!selected||busy)return;const title=selected.title||"this session";if(!window.confirm(`Delete “${title}” and its saved history? This cannot be undone.`))return;try{await agentApi.deleteSession(selected.id);setSessions(items=>items.filter(item=>item.id!==selected.id));setSessionId("");setHistory([]);setEvents([]);setPlan(null);setTasks([]);setChangesets([]);setActiveChangesetId("");setAnswer("");setChatError("");}catch(e){onError(String(e));}};
  const act=async(action:"pause"|"resume"|"cancel")=>{if(!sessionId)return;onSessionControl?.(action,sessionId);try{const s=await agentApi.control(sessionId,action);setSessions(list=>list.map(x=>x.id===s.id?s:x));}catch(e){onError(String(e));}};
  const decide=async(approved:boolean)=>{if(!sessionId)return;try{const r=await agentApi.approve(sessionId,approved);setSessions(list=>list.map(x=>x.id===r.session.id?r.session:x));await refreshSession(sessionId);}catch(e){onError(String(e));}};
  const activeChangeset=changesets.find(item=>item.id===activeChangesetId)||changesets[0];
  const activeChange=activeChangeset?.changes.find(item=>item.id===activeChangeId)||activeChangeset?.changes[0];
  const updateChangeset=(item:AgentChangeSet)=>setChangesets(current=>[item,...current.filter(old=>old.id!==item.id)]);
  const generateChanges=async()=>{
    if(!sessionId||busy||generatingChangesRef.current)return;
    generatingChangesRef.current=true;setBusy(true);setActivityError("");setGenerationFailed(false);
    try{
      const item=await agentApi.generateChanges(sessionId,roles.coding||planner,unsavedPaths);
      updateChangeset(item);setActiveChangesetId(item.id);setActiveChangeId(item.changes[0]?.id||"");
      try{await refreshSession(sessionId);}catch(error){setActivityError(String(error));onError(String(error));}
    }catch(error){setActivityError(String(error));setGenerationFailed(true);onError(String(error));try{const recovered=await refreshSession(sessionId);if(recovered.length)setGenerationFailed(false);}catch{/* Keep the generation error visible if the proposal could not be recovered. */}}
    finally{generatingChangesRef.current=false;setBusy(false);}
  };
  const approveAndBuild=async()=>{
    if(!sessionId||busy)return;
    try{const result=await agentApi.approve(sessionId,true);setSessions(list=>list.map(item=>item.id===result.session.id?result.session:item));await refreshSession(sessionId);await generateChanges();}
    catch(error){setActivityError(String(error));onError(String(error));}
  };
  const reviewChange=async(change:AgentFileChange,decision:"approved"|"rejected")=>{if(!sessionId||!activeChangeset)return;try{const result=await agentApi.reviewChange(sessionId,activeChangeset.id,change.id,decision);updateChangeset(result);}catch(e){onError(String(e));}};
  const reviewAllChanges=async(decision:"approved"|"rejected")=>{if(!sessionId||!activeChangeset)return;try{const result=await agentApi.reviewAllChanges(sessionId,activeChangeset.id,decision);updateChangeset(result);}catch(e){onError(String(e));}};
  const applyChanges=async()=>{if(!sessionId||!activeChangeset)return;const approved=activeChangeset.changes.filter(item=>item.status==="approved");if(!approved.length)return;const destructive=approved.some(item=>item.operation==="delete"||item.operation==="rename");if(!window.confirm(`Apply ${approved.length} explicitly approved file change(s)?${destructive?" This includes a file deletion or rename.":" The current files will be snapshotted first."}`))return;setBusy(true);try{const result=await agentApi.applyChanges(sessionId,activeChangeset.id,unsavedPaths);updateChangeset(result);await onFilesApplied?.();await refreshSession(sessionId);}catch(e){onError(String(e));}finally{setBusy(false);}};
  const rollbackChanges=async()=>{if(!sessionId||!activeChangeset)return;if(!window.confirm("Roll back this changeset? Rollback will stop if an affected file was edited after apply."))return;setBusy(true);try{const result=await agentApi.rollbackChanges(sessionId,activeChangeset.id,unsavedPaths);updateChangeset(result);await onFilesApplied?.();await refreshSession(sessionId);}catch(e){onError(String(e));}finally{setBusy(false);}};
  const scan=async()=>{setBusy(true);try{const excludePaths=exclusions.split(",").map(x=>x.trim()).filter(Boolean);const started=await agentApi.scanProject(excludePaths);setScanJobId(started.job_id);setScanProgress({completed:0,total:null,current_file:""});for(;;){await new Promise(resolve=>window.setTimeout(resolve,500));const state=await agentApi.scanStatus(started.job_id);setScanProgress({completed:state.completed,total:state.total,current_file:state.current_file});if(["indexed","failed","cancelled"].includes(state.status)){setScanJobId("");if(state.result){setStatus({status:"indexed",files_indexed:state.result.files_indexed,indexed_at:new Date().toISOString(),summary:state.result.summary,scan:{embedding:state.result.embedding}});setSummary(state.result.summary);}else{setStatus(await agentApi.projectStatus());if(state.error)onError(state.error);}break;}}}catch(e){setScanJobId("");onError(String(e));}finally{setBusy(false);}};
  const cancelScan=async()=>{if(scanJobId)try{await agentApi.cancelScan(scanJobId);}catch(e){onError(String(e));}};
  const loadSummary=async()=>{try{setSummary(await agentApi.projectSummary());}catch(e){onError(String(e));}};
  const loadDependencies=async()=>{try{setDependencies((await agentApi.projectDependencies()).dependencies);}catch(e){onError(String(e));}};
  const search=async()=>{if(!query.trim())return;try{setResults((await agentApi.projectSearch(query,searchMode)).results);}catch(e){onError(String(e));}};
  const assignRole=async(role:string,modelId:string)=>{try{await agentApi.setRole(role,modelId||null);setRoles(old=>({...old,[role]:modelId}));if(role==="planner")setPlanner(modelId);}catch(e){onError(String(e));}};
  const manageRuntime=async(role:"chat"|"coding")=>{
    const current=runtimeStates[role];if(!current||runtimeBusy)return;
    setRuntimeBusy(role);setRuntimeError("");
    try{
      if(current.available&&current.managed_by_app){await agentApi.stopManagedRuntime(role);setRuntimeStates(await agentApi.managedRuntimes());return;}
      if(current.available)return;
      await agentApi.startManagedRuntime(role);
      for(let attempt=0;attempt<180;attempt++){
        await new Promise(resolve=>window.setTimeout(resolve,1000));
        const next=await agentApi.managedRuntimes();setRuntimeStates(next);
        if(next[role].available)return;
        if(!next[role].process_running&&attempt>1)throw new Error(next[role].message||`${next[role].name} stopped before becoming ready.`);
      }
      throw new Error(`${current.name} did not become ready within three minutes.`);
    }catch(error){setRuntimeError(String(error));onError(String(error));}
    finally{setRuntimeBusy("");}
  };
  const taskCounts=tasks.reduce((counts,task)=>{counts[task.status]=(counts[task.status]||0)+1;return counts;},{} as Record<string,number>);
  const agentBusy=tab==="agent"&&Boolean(selected)&&busy;
  const externalExecution=tab==="agent"&&Boolean(selected)&&running&&!busy;
  const agentState=debugStage?.startsWith("Executing approved test command")?"Testing":selected?.status==="paused"?"Paused":selected?.status==="failed"?"Failed":selected?.status==="planning"?"Planning":selected?.status==="waiting_for_approval"||planStatus==="waiting_for_approval"?"Awaiting Approval":externalExecution?"Executing":selected?.status==="plan_approved"&&agentBusy?"Executing":tasks.some(task=>task.status==="in_progress")?"Executing":tasks.length>0&&tasks.every(task=>task.status==="completed")?"Completed":agentBusy?(selected?.mode==="planning"?"Planning":"Thinking"):"Idle";
  const planSummary=selected?.status==="failed"?"failed":selected?.status==="cancelled"?"cancelled":selected?.status==="paused"?"paused":tasks.length>0&&tasks.every(task=>task.status==="completed")?"completed":tasks.some(task=>task.status==="in_progress")?"in progress":selected?.status==="planning"?"planning":selected?.status==="waiting_for_approval"?"awaiting approval":planStatus.replace(/_/g," ")||(plan?"ready":"No plan");
  const latestEvent=events[events.length-1];
  const stateClass=agentState.toLowerCase().replace(/\s+/g,"-");
  return <div className={`agent-backdrop ${embedded?"agent-embedded":""}`} role="presentation" onMouseDown={e=>{if(!embedded&&e.target===e.currentTarget&&!scanJobId)onClose();}}>
    <section className={`agent-window ${embedded?"agent-window-embedded":""}`} role={embedded?"region":"dialog"} aria-modal={embedded?undefined:true} aria-label="Autonomous AI Engineer">
      <header className="agent-header"><div className="agent-header-title"><Brain size={17}/><div><strong>Autonomous AI Engineer</strong><small title={sessionId||undefined}>{selected?`${selected.title} · ${selected.project_root?.split(/[\\/]/).pop()||"Project"}`:"No session selected"}</small></div><span className={`agent-state agent-state-${stateClass}`} title={sessionId||undefined}><i/>{agentState}</span></div><div className="agent-header-actions">{embedded&&<button aria-label={focused?"Return to standard panel width":"Expand Agent panel"} title={focused?"Return to standard panel width":"Expand Agent panel"} onClick={onToggleFocus}>{focused?<Minimize2 size={15}/>:<Maximize2 size={15}/>}</button>}<button className="agent-close-button" aria-label="Close Agent panel" title="Hide Agent panel" onClick={onClose}><X size={16}/></button></div></header>
      <nav className="agent-tabs"><button className={tab==="agent"?"active":""} onClick={()=>setTab("agent")}>Agent</button><button className={tab==="project"?"active":""} onClick={()=>setTab("project")}>Project intelligence</button></nav>
      {tab==="agent"?<div className="agent-content">
        <div className="agent-toolbar agent-chat-model-toolbar"><label className="agent-model-label"><span>Chat model</span><select aria-label="Chat model" value={planner} onChange={e=>void assignRole("planner",e.target.value)}><option value="">{models.length?"Choose a local model":"No local models available"}</option>{models.map(m=><option key={m.id} value={m.id}>{m.name} · {m.runtime}</option>)}</select></label><button className="agent-runtime-button" disabled={runtimeBusy==="chat"||Boolean(runtimeStates.chat?.available&&!runtimeStates.chat?.managed_by_app)||Boolean(runtimeStates.chat?.process_running&&!runtimeStates.chat?.available)} onClick={()=>void manageRuntime("chat")} title={runtimeStates.chat?.available&&!runtimeStates.chat?.managed_by_app?"A matching chat server is already running outside the app":runtimeStates.chat?.managed_by_app?"Stop the chat model managed by the app":`Load ${runtimeStates.chat?.name||"chat model"} in the app`}>{runtimeBusy==="chat"?"Starting…":runtimeStates.chat?.available?runtimeStates.chat.managed_by_app?"Stop":"External":runtimeStates.chat?.process_running?"Loading…":"Load"}</button><button className="agent-icon-button" onClick={()=>void refreshSessions()} title="Refresh models, sessions and runtime status" aria-label="Refresh models, sessions and runtime status"><RefreshCw size={14}/></button></div>
        <div className="agent-toolbar agent-coding-model-toolbar"><label className="agent-model-label"><span>Coding model</span><select aria-label="Coding model" value={roles.coding||""} onChange={e=>void assignRole("coding",e.target.value)}><option value="">{models.length?"Use chat model":"No local models available"}</option>{models.map(m=><option key={m.id} value={m.id}>{m.name} · {m.runtime}</option>)}</select></label><button className="agent-runtime-button" disabled={runtimeBusy==="coding"||Boolean(runtimeStates.coding?.available&&!runtimeStates.coding?.managed_by_app)||Boolean(runtimeStates.coding?.process_running&&!runtimeStates.coding?.available)} onClick={()=>void manageRuntime("coding")} title={runtimeStates.coding?.available&&!runtimeStates.coding?.managed_by_app?"A matching coding server is already running outside the app":runtimeStates.coding?.managed_by_app?"Stop the coding model managed by the app":`Load ${runtimeStates.coding?.name||"coding model"} in the app`}>{runtimeBusy==="coding"?"Starting…":runtimeStates.coding?.available?runtimeStates.coding.managed_by_app?"Stop":"External":runtimeStates.coding?.process_running?"Loading…":"Load"}</button></div>
        {runtimeError&&<p className="agent-error agent-chat-error" role="alert">{runtimeError}</p>}
        <div className="agent-session-row"><div className="agent-session-picker"><button type="button" className="agent-session-picker-trigger" aria-label="Session history" aria-expanded={sessionDropdownOpen} onClick={()=>setSessionDropdownOpen(value=>!value)}><span><strong>{selected?(selected.title&&!/^new session$/i.test(selected.title)?selected.title:(selected.requirement||"Untitled session").replace(/\s+/g," ").slice(0,64)):"Select session history"}</strong><small>{selected?sessionTimeLabel(selected.created_at):"Choose a saved session"}</small></span><ChevronDown size={14}/></button>{sessionDropdownOpen&&<div className="agent-session-picker-menu" role="listbox" aria-label="Saved sessions">{sessions.map(item=><button type="button" role="option" aria-selected={item.id===sessionId} key={item.id} onClick={()=>void loadSession(item.id)}><strong>{item.title&&!/^new session$/i.test(item.title)?item.title:(item.requirement||"Untitled session").replace(/\s+/g," ").slice(0,64)}</strong><small>{sessionTimeLabel(item.created_at)}</small></button>)}{sessions.length===0&&<small className="agent-session-picker-empty">No saved sessions</small>}</div>}</div><select aria-label="Mode for new session" value={mode} onChange={e=>setMode(e.target.value as "ask"|"planning")}><option value="ask">Ask</option><option value="planning">Build</option></select><button disabled={busy||!planner} onClick={()=>void create()} title="Create a new session">New</button>{busy&&<button className="agent-icon-button" onClick={stopChat} title="Stop the running chat request" aria-label="Stop chat"><Square size={13}/></button>}{selected&&<button className="agent-icon-button" onClick={()=>{const next=window.prompt("Session title",selected.title);if(next!==null)void renameSession(next);}} title="Rename session" aria-label="Rename session"><Pencil size={13}/></button>}{selected&&selected.status==="paused"?<button className="agent-icon-button" onClick={()=>void act("resume")} title="Resume session" aria-label="Resume session"><Play size={14}/></button>:selected&&!(["failed","cancelled","plan_rejected","ready","awaiting_input"].includes(selected.status))&&<button className="agent-icon-button" onClick={()=>void act("pause")} title="Pause session" aria-label="Pause session"><Pause size={14}/></button>}{selected&&!(["failed","cancelled","plan_rejected","ready","awaiting_input"].includes(selected.status))&&<button className="agent-icon-button" onClick={()=>void act("cancel")} title="Cancel session" aria-label="Cancel session"><X size={14}/></button>}{selected&&<button className="agent-icon-button" disabled={busy} onClick={()=>void deleteSession()} title="Delete session" aria-label="Delete session"><Trash2 size={13}/></button>}</div>
        {debugStage&&<div className="agent-debug-stage"><strong>Testing / debugging</strong><span>{debugStage}</span></div>}
        {chatError&&<p className="agent-error agent-chat-error" role="alert">{chatError}</p>}
        {activityError&&<p className="agent-error agent-chat-error" role="alert">{activityError}</p>}
        <div className="agent-conversation" ref={conversationRef} onScroll={event=>{const node=event.currentTarget;stickConversationToBottom.current=node.scrollHeight-node.scrollTop-node.clientHeight<48;}} aria-live="polite">
          {!sessionId&&<div className="agent-empty"><Brain size={24}/><strong>Start a conversation</strong><p>Select a model and create a session to ask a question or plan project changes.</p></div>}
          {sessionId&&history.length===0&&!selected?.requirement&&!answer&&!pendingPrompt&&<div className="agent-empty"><Brain size={22}/><strong>Ready when you are</strong><p>Ask about the project or switch to Build to prepare reviewed file changes.</p></div>}
          {selected?.mode==="planning"&&selected.requirement&&!pendingPrompt&&!history.some(message=>message.role==="user"&&message.content===selected.requirement)&&<article className="agent-message agent-message-user"><header><strong>You · Build request</strong><time>{new Date(selected.updated_at).toLocaleTimeString()}</time></header><div className="agent-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{selected.requirement}</ReactMarkdown></div></article>}
          {history.map(message=><article className={`agent-message ${message.role==="user"?"agent-message-user":"agent-message-assistant"}`} key={message.id}><header><strong>{message.role==="user"?"You":"Agent"}</strong><time>{new Date(message.created_at).toLocaleTimeString()}</time></header><div className="agent-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown></div>{message.role==="assistant"&&message.content===answer&&answerEvidence.length>0&&<div className="agent-evidence"><strong>Project sources</strong>{answerEvidence.map((item,index)=><small key={`${item.path}-${item.line_start}-${index}`}>{item.path}:{item.line_start}-{item.line_end}</small>)}</div>}</article>)}
          {answer&&history[history.length-1]?.content!==answer&&<article className="agent-message agent-message-assistant"><header><strong>Agent</strong><time>Just now</time></header><div className="agent-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{answer}</ReactMarkdown></div></article>}
          {pendingPrompt&&!history.some(message=>message.role==="user"&&message.content===pendingPrompt)&&<article className="agent-message agent-message-user"><header><strong>You</strong><time>Just now</time></header><div className="agent-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{pendingPrompt}</ReactMarkdown></div></article>}
          {busy&&<div className="agent-thinking" role="status"><span/><span/><span/>{selected?.mode==="planning"?"Planning against the project…":"Thinking…"}</div>}
          {selected?.status==="failed"&&selected.mode==="ask"&&<div className="agent-error" role="alert">{selected.error||"The request did not complete. Your existing conversation is still available."}</div>}
        </div>
        <details className="agent-drawer agent-plan-drawer">
          <summary><span className="agent-drawer-title">PLAN</span><span className="agent-drawer-summary">{planSummary}{tasks.length?` · ${tasks.length} tasks`:""}</span><ChevronDown size={14}/></summary>
          <div className="agent-drawer-body">
            {selected?.error&&selected.status!=="failed"&&<p className="agent-error" role="status">{selected.error}</p>}
            {debugStage&&<div className="agent-debug-stage"><strong>Testing / debugging</strong><span>{debugStage}</span></div>}
            {debugDiagnosis&&<section className="agent-card agent-debug-diagnosis"><h3>Failure diagnosis</h3><p>{debugDiagnosis.root_cause||"The model could not determine a root cause."}</p><small>Confidence: {Math.round((debugDiagnosis.confidence||0)*100)}%</small>{debugDiagnosis.alternatives?.length? <><h4>Alternative explanations</h4><ul>{debugDiagnosis.alternatives.map((item,index)=><li key={index}>{item}</li>)}</ul></>:null}{debugDiagnosis.evidence?.map(item=><article className="agent-result" key={`${item.path}-${item.line_start}`}><small>{item.path}:{item.line_start}-{item.line_end}</small><pre>{item.excerpt}</pre></article>)}</section>}
            {!plan&&selected?.status==="planning"&&<p className="agent-plan-state" role="status">Plan generation is in progress.</p>}
            {!plan&&selected?.mode==="ask"&&<p className="agent-plan-state">This Ask session has no plan. Choose Build when creating a session to prepare project changes.</p>}
            {selected?.status==="failed"&&selected.mode==="planning"&&!plan&&<section className="agent-card agent-retry-card" role="status"><strong>Planning failed</strong><p>{selected.error||"No plan was saved for this session."}</p><span>The original requirement and project association are retained.</span><button onClick={()=>void retryPlanning()} disabled={busy}>{busy?"Retrying plan…":"Retry Planning"}</button></section>}
            {plan&&<section className="agent-card agent-plan-card"><h3>{plan.objective}</h3><p>{plan.architecture_overview}</p><small>Complexity: {plan.estimated_complexity} · {plan.technology_stack?.join(", ")}</small>{tasks.length>0&&<small>{taskCounts.completed||0} of {tasks.length} tasks completed</small>}{plan.project_structure?.length? <><h4>Proposed folder structure</h4><ul className="agent-project-structure">{plan.project_structure.map(item=><li key={item.path}><code>{item.path}</code>{item.purpose&&<small>{item.purpose}</small>}</li>)}</ul></>:null}<h4>Plan tasks</h4><ol>{tasks.map(task=><li key={task.id} className={task.parent_id?"agent-subtask":""}><strong>{task.title}</strong><span className="task-state">{task.status.replace(/_/g," ")}</span>{task.description&&<p>{task.description}</p>}{task.dependencies.length>0&&<small>Depends on {task.dependencies.map(id=>tasks.find(item=>item.id===id)?.title||id).join(", ")}</small>}</li>)}</ol>{Array.isArray(plan.assumptions)&&plan.assumptions.length>0&&<><h4>Assumptions</h4><ul>{plan.assumptions.map((item:string,index:number)=><li key={index}>{item}</li>)}</ul></>}{Array.isArray(plan.risks)&&plan.risks.length>0&&<><h4>Risks</h4><ul>{plan.risks.map((item:string,index:number)=><li key={index}>{item}</li>)}</ul></>}{planStatus==="waiting_for_approval"&&<div className="agent-actions"><button onClick={()=>void approveAndBuild()} disabled={busy}><Check size={14}/> Allow plan and start build</button><button onClick={()=>void decide(false)}><X size={14}/> Deny plan</button></div>}</section>}
            {planStatus==="approved"&&<section className="agent-card agent-generate"><strong>Plan approved</strong><span>Generate a coordinated file proposal. Files are written only after review and approval. Local model generation can take several minutes for full-file changes.</span>{plan?.commands?.length? <div className="agent-plan-commands"><h4>Suggested project commands</h4>{(plan?.commands||[]).map((item,index)=><div key={`${item.command}-${index}`}><code>{item.command}</code>{item.reason&&<small>{item.reason}</small>}<button disabled={busy||!onProposeCommand} onClick={()=>onProposeCommand?.(item.command,sessionId)}>Review command</button></div>)}</div>:null}<button disabled={busy||generatingChangesRef.current} onClick={()=>void generateChanges()}>{busy?"Preparing changes…":generationFailed?"Retry file generation":"Generate file changes"}</button>{busy&&<div className="agent-generation-progress" role="status"><i/><span>Reading approved tasks and preparing review diffs…</span></div>}<label className="agent-command-label">Verification command <small>This opens a preview; it will not run until approved.</small><textarea value={agentCommand} onChange={event=>setAgentCommand(event.target.value)} placeholder={plan?.verification_command||"Enter a project-specific command to review."}/></label><button disabled={!agentCommand.trim()||busy} onClick={()=>onProposeCommand?.(agentCommand.trim(),sessionId)}>Review command in Terminal</button></section>}
            {activeChangeset&&<section className="agent-card agent-change-review"><header><div><h3>File change review</h3><small>{activeChangeset.status.replace(/_/g," ")} · {activeChangeset.changes.length} file(s)</small></div>{changesets.length>1&&<select aria-label="Change proposal" value={activeChangeset.id} onChange={event=>{setActiveChangesetId(event.target.value);setActiveChangeId("");}}>{changesets.map(item=><option key={item.id} value={item.id}>{new Date(item.created_at).toLocaleTimeString()} · {item.status}</option>)}</select>}</header><div className="agent-change-list">{activeChangeset.changes.map(change=><button key={change.id} className={activeChange?.id===change.id?"active":""} onClick={()=>setActiveChangeId(change.id)}><span><b>{change.operation.toUpperCase()}</b> {change.path}{change.new_path?` → ${change.new_path}`:""}</span><small>{change.status}</small></button>)}</div>{activeChange&&<><div className="agent-change-summary"><strong>{activeChange.summary||activeChange.path}</strong><span>{activeChange.task_ids.length?`Tasks: ${activeChange.task_ids.join(", ")}`:"No plan task link returned"}</span></div><div className="agent-change-diff"><DiffEditor height="100%" language={activeChange.path.split(".").pop()||"plaintext"} original={activeChange.original_content||""} modified={activeChange.content??(activeChange.operation==="rename"?activeChange.original_content||"":"")} theme={editorTheme} options={{readOnly:true,renderSideBySide:true,minimap:{enabled:false},automaticLayout:true,originalEditable:false,scrollBeyondLastLine:false}}/></div><div className="agent-file-review-actions">{activeChange.status==="pending"&&<><button onClick={()=>void reviewChange(activeChange,"rejected")}><X size={13}/> Deny file</button><button onClick={()=>void reviewChange(activeChange,"approved")}><Check size={13}/> Allow file</button></>}{activeChange.status==="approved"&&<button onClick={()=>void reviewChange(activeChange,"rejected")}><X size={13}/> Revoke approval</button>}{activeChangeset.changes.some(item=>["pending","approved"].includes(item.status))&&<><button onClick={()=>void reviewAllChanges("rejected")}>Deny all</button><button onClick={()=>void reviewAllChanges("approved")}>Allow all pending</button></>}</div></>}{activeChangeset.changes.some(item=>item.status==="approved")&&<button className="agent-apply-button" disabled={busy} onClick={()=>void applyChanges()}>Apply approved files</button>}{["applied","partially_applied"].includes(activeChangeset.status)&&<button className="agent-apply-button" disabled={busy} onClick={()=>void rollbackChanges()}>Rollback applied files</button>}</section>}
          </div>
        </details>
        <details className="agent-drawer agent-activity-drawer">
          <summary><span className="agent-drawer-title">ACTIVITY</span><span className="agent-drawer-summary">{latestEvent?.message||"No activity yet"}{events.length?` · ${events.length}`:""}</span><ChevronDown size={14}/></summary>
          <div className="agent-drawer-body agent-activity-list">{events.map(event=>{const failed=event.event_type.includes("failed");const complete=/approved|completed|passed|applied/.test(event.event_type);const Icon=failed?AlertCircle:complete?CheckCircle2:Circle;return <details className={`agent-activity-event ${failed?"failed":""}`} key={event.id}><summary><Icon size={13}/><time>{new Date(event.created_at).toLocaleString()}</time><span>{event.message}</span></summary><p>{event.message}</p></details>;})}{!events.length&&<p className="agent-no-activity">No recorded events for this session.</p>}</div>
        </details>
        <div className="agent-compose"><textarea aria-label="Message the Agent" value={requirement} onChange={e=>setRequirement(e.target.value)} onKeyDown={event=>{if(event.key==="Enter"&&!event.shiftKey&&!event.nativeEvent.isComposing){event.preventDefault();void submit();}}} placeholder="Ask the Agent to work on your project..."/><button disabled={busy||!sessionId||!requirement.trim()||selected?.status==="paused"||selected?.status==="cancelled"} onClick={()=>void submit()} title="Send message"><Send size={15}/><span>{busy?"Sending":"Send"}</span></button></div>
      </div>:<div className="agent-content project-intel">
        <details className="agent-exclusions"><summary>Custom scan exclusions</summary><input value={exclusions} onChange={e=>setExclusions(e.target.value)} placeholder="Comma-separated paths or names, e.g. fixtures, generated/client"/><small>Built-in secret, binary, cache and dependency exclusions always apply.</small></details>
        <div className="project-intel-actions"><div><b>Index status:</b> {status.status.replace(/_/g," ")} · {status.files_indexed} files{status.scan?.embedding&&<small> · Embeddings: {status.scan.embedding.status}{status.scan.embedding.total_embeddings?` (${status.scan.embedding.total_embeddings})`:""}</small>}{status.indexed_at&&<small> · Updated {new Date(status.indexed_at).toLocaleString()}</small>}</div>{scanJobId?<button onClick={()=>void cancelScan()}><X size={14}/> Cancel scan</button>:<button disabled={busy} onClick={()=>void scan()}><ScanSearch size={14}/>{busy?"Starting…":"Scan and index"}</button>}</div>
        {scanJobId&&<div className="agent-scan-progress"><progress max={scanProgress.total||undefined} value={scanProgress.total?scanProgress.completed:undefined}/><span>{scanProgress.total?`${scanProgress.completed} / ${scanProgress.total} files`:"Enumerating project files…"}</span>{scanProgress.current_file&&<small>{scanProgress.current_file}</small>}</div>}
        {summary&&<section className="agent-card"><h3>{String(summary.project_name||"Project summary")}</h3><p>Type: {Array.isArray(summary.project_type)?summary.project_type.join(", "):"Unknown"}</p><p>Languages: {JSON.stringify(summary.languages||{})}</p><p>Frameworks: {Array.isArray(summary.frameworks)?summary.frameworks.join(", "):"None detected"}</p><p>Directory roots: {Array.isArray(summary.directory_structure)?summary.directory_structure.join(", "):"None detected"}</p><p>Entry points: {Array.isArray(summary.entry_points)?summary.entry_points.join(", "):"None detected"}</p><p>Important modules: {Array.isArray(summary.important_modules)?summary.important_modules.join(", "):"No symbols detected"}</p><p>Manifests: {Array.isArray(summary.dependency_manifests)?summary.dependency_manifests.join(", "):"None detected"}</p><p>Detected dependencies: {Array.isArray(summary.dependencies)?summary.dependencies.slice(0,50).join(", "):"None detected"}</p><p>Database signals: {Array.isArray(summary.database_signals)?summary.database_signals.join(", "):"None detected"}</p><p>Run instructions: {Array.isArray(summary.run_instructions)?summary.run_instructions.join(", "):"None detected"}</p><button onClick={()=>void loadDependencies()}>Show dependency relationships</button></section>}
        {dependencies.length>0&&<section className="agent-card"><h4>Dependency relationships</h4>{dependencies.slice(0,200).map((edge,i)=><p className="agent-dependency" key={`${edge.source_path}-${edge.target}-${i}`}><strong>{edge.source_path}</strong> → {edge.target} <small>{edge.resolution}</small></p>)}</section>}
        <div className="agent-search"><input value={query} onChange={e=>setQuery(e.target.value)} onKeyDown={e=>{if(e.key==="Enter")void search();}} placeholder="Search files and indexed symbols"/><select aria-label="Search mode" value={searchMode} onChange={e=>setSearchMode(e.target.value as "keyword"|"semantic"|"hybrid")}><option value="keyword">Keyword</option><option value="semantic">Semantic</option><option value="hybrid">Hybrid</option></select><button onClick={()=>void search()}><Search size={14}/> Search</button><button onClick={()=>void loadSummary()} disabled={status.status!=="indexed"}>Summary</button></div>
        <div className="agent-scroll">{results.map((r,i)=><article className="agent-result" key={`${r.path}-${r.line_start}-${i}`}><strong>{r.name||r.path}</strong><small>{r.path}{r.line_start?`:${r.line_start}`:""} · {r.type}</small><pre>{r.excerpt}</pre></article>)}{!results.length&&<div className="agent-empty"><Search size={26}/><p>Index the selected workspace to inspect project structure and search source symbols.</p></div>}</div>
      </div>}
    </section>
  </div>;
}
