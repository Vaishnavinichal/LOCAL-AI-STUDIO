import { useCallback, useEffect, useState } from "react";
import { FolderOpen, Plus, Search, ArrowLeft, MessageSquare, Trash2, ExternalLink, LoaderCircle } from "lucide-react";
import { chatApi } from "../../services/api";
import type { Project, ProjectMessage } from "../../types/chat";

interface Props {
  refreshKey: number;
  onOpenConversation: (conversationId: string) => void;
  onError: (message: string) => void;
  onSuccess: (message: string) => void;
}

export default function ProjectsWorkspace({ refreshKey, onOpenConversation, onError, onSuccess }: Props) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selected, setSelected] = useState<Project | null>(null);
  const [projectMessages, setProjectMessages] = useState<ProjectMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [query, setQuery] = useState("");

  const loadProjects = useCallback(async () => {
    setLoading(true);
    try {
      const data = await chatApi.listProjects();
      setProjects(data);
      if (selected) {
        const updated = data.find(project => project.id === selected.id);
        if (updated) setSelected(updated);
        else { setSelected(null); setProjectMessages([]); }
      }
    } catch (err) {
      onError(err instanceof Error ? err.message : "Unable to load projects");
    } finally { setLoading(false); }
  }, [onError, selected?.id]);

  useEffect(() => { loadProjects(); }, [loadProjects, refreshKey]);

  async function openProject(project: Project) {
    setSelected(project);
    setLoading(true);
    try {
      const data = await chatApi.getProjectMessages(project.id);
      setProjectMessages(data);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Unable to load project responses");
    } finally { setLoading(false); }
  }

  async function createProject(event: React.FormEvent) {
    event.preventDefault();
    const name = newName.trim();
    if (!name) return;
    try {
      const created = await chatApi.createProject(name);
      setNewName(""); setCreating(false);
      const updated = await chatApi.listProjects();
      setProjects(updated); setSelected(created); setProjectMessages([]);
      onSuccess(`Project "${created.name}" created.`);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Unable to create project");
    }
  }

  async function removeResponse(message: ProjectMessage) {
    if (!selected || !window.confirm("Remove this response from the project? The original chat message will remain محفوظ.")) return;
    try {
      await chatApi.removeFromProject(message.id, selected.id);
      setProjectMessages(previous => previous.filter(item => item.id !== message.id));
      onSuccess("Response removed from project.");
    } catch (err) {
      onError(err instanceof Error ? err.message : "Unable to remove response");
    }
  }

  const filteredProjects = projects.filter(project => project.name.toLowerCase().includes(query.toLowerCase()));

  return (
    <main className="projects-page">
      <header className="projects-topbar">
        {selected ? <button className="projects-back" onClick={() => setSelected(null)}><ArrowLeft size={17} /> All projects</button> : <span>Projects</span>}
        <span className="projects-local-tag"><FolderOpen size={14} /> Stored locally</span>
      </header>

      {selected ? (
        <section className="project-detail">
          <div className="project-heading">
            <div className="project-icon large"><FolderOpen size={25} /></div>
            <div><h1>{selected.name}</h1><p>Saved assistant responses in this project</p></div>
          </div>
          {loading ? <div className="projects-loading"><LoaderCircle className="spinner" /> Loading project...</div> :
            projectMessages.length === 0 ? <div className="projects-empty"><MessageSquare size={28} /><h3>No saved responses yet</h3><p>Use “Add to Project” on an assistant response in a chat to save it here.</p></div> :
            <div className="project-response-list">
              {projectMessages.map(item => <article className="project-response-card" key={item.id}>
                <div className="project-response-meta"><span><MessageSquare size={14} /> Assistant response</span><time>{new Date(item.created_at).toLocaleString()}</time></div>
                <p className="project-response-content">{item.content}</p>
                <div className="project-response-actions">
                  <button onClick={() => onOpenConversation(item.conversation_id)}><ExternalLink size={15} /> Open original chat</button>
                  <button className="remove-response" onClick={() => removeResponse(item)}><Trash2 size={15} /> Remove from project</button>
                </div>
              </article>)}
            </div>
          }
        </section>
      ) : (
        <section className="projects-list-view">
          <div className="projects-title-row"><div><h1>Your Projects</h1><p>Organize useful AI responses into local project spaces.</p></div>
            <button className="create-project-button" onClick={() => setCreating(value => !value)}><Plus size={17} /> New Project</button>
          </div>
          {creating && <form className="create-project-form" onSubmit={createProject}>
            <input autoFocus value={newName} onChange={event => setNewName(event.target.value)} placeholder="Project name" maxLength={100} />
            <button type="submit" disabled={!newName.trim()}>Create</button>
            <button type="button" className="cancel-create" onClick={() => { setCreating(false); setNewName(""); }}>Cancel</button>
          </form>}
          <div className="projects-search"><Search size={16} /><input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search projects..." /></div>
          {loading ? <div className="projects-loading"><LoaderCircle className="spinner" /> Loading projects...</div> :
            filteredProjects.length === 0 ? <div className="projects-empty"><FolderOpen size={30} /><h3>{projects.length ? "No matching projects" : "No projects yet"}</h3><p>Create a project, then save assistant responses from your chats.</p></div> :
            <div className="projects-grid">{filteredProjects.map(project => <button className="project-card" key={project.id} onClick={() => openProject(project)}>
              <div className="project-card-icon"><FolderOpen size={22} /></div>
              <strong>{project.name}</strong>
              <span>Open project <ExternalLink size={13} /></span>
            </button>)}</div>
          }
        </section>
      )}
    </main>
  );
}
