import { useCallback, useEffect, useRef, useState } from "react";
import { FileText, FileUp, File as FileIcon, Trash2, RefreshCw, ShieldCheck, Search, ScanText, Loader2 } from "lucide-react";
import { documentsApi, documentStudyApi, type LocalDocument, type StudyQuestion, type StudyFlashcard, type StudyGlossaryItem, type StudyMaterial } from "../../services/api";
import { subscribeToNativeFileDrop } from "../../services/platform";
import "./documents.css";

interface Props { onError: (message: string) => void; onSuccess: (message: string) => void; }
const MAX_BYTES = 50 * 1024 * 1024;
function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
function formatDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}
export default function DocumentsWorkspace({ onError, onSuccess }: Props) {
  const [documents, setDocuments] = useState<LocalDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [ocrRunning, setOcrRunning] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [studyDocumentId, setStudyDocumentId] = useState("");
  const [studyTab, setStudyTab] = useState<"ask" | "synopsis" | "questions" | "quiz" | "flashcards" | "glossary" | "concept" | "notes">("ask");
  const [studyBusy, setStudyBusy] = useState(false);
  const [studyError, setStudyError] = useState("");
  const [studyQuestion, setStudyQuestion] = useState("");
  const [studyAnswer, setStudyAnswer] = useState<{ answer: string; sources: number[]; grounded: boolean } | null>(null);
  const [synopsis, setSynopsis] = useState("");
  const [synopsisStyle, setSynopsisStyle] = useState<"short" | "detailed" | "chapter" | "topic">("detailed");
  const [questionCount, setQuestionCount] = useState(10);
  const [difficulty, setDifficulty] = useState("mixed");
  const [questionType, setQuestionType] = useState("mixed");
  const [studyQuestions, setStudyQuestions] = useState<StudyQuestion[]>([]);
  const [revealedAnswers, setRevealedAnswers] = useState<number[]>([]);
  const [quizAnswers, setQuizAnswers] = useState<Record<number, string>>({});
  const [quizSubmitted, setQuizSubmitted] = useState(false);
  const [flashcards, setFlashcards] = useState<StudyFlashcard[]>([]);
  const [flashIndex, setFlashIndex] = useState(0);
  const [flashRevealed, setFlashRevealed] = useState(false);
  const [glossary, setGlossary] = useState<StudyGlossaryItem[]>([]);
  const [conceptMap, setConceptMap] = useState("");
  const [noteTitle, setNoteTitle] = useState("");
  const [noteContent, setNoteContent] = useState("");
  const [savedMaterials, setSavedMaterials] = useState<StudyMaterial[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const [preview, setPreview] = useState<{ filename: string; text: string; status: string; page_count: number | null; pdfUrl?: string } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => () => { if (preview?.pdfUrl) URL.revokeObjectURL(preview.pdfUrl); }, [preview?.pdfUrl]);

  const refresh = useCallback(async () => {
    try { setDocuments(await documentsApi.list()); }
    catch (error) { onError(error instanceof Error ? error.message : "Unable to load documents"); }
    finally { setLoading(false); }
  }, [onError]);
  useEffect(() => { void refresh(); }, [refresh]);

  // Tauri uses native OS paths; browsers continue to use the DOM FileList drop handler.
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    let cancelled = false;
    void subscribeToNativeFileDrop(async event => {
      if (event.type === "enter" || event.type === "over") {
        setDragActive(true);
      } else if (event.type === "leave") {
        setDragActive(false);
      } else if (event.type === "drop") {
        setDragActive(false);
        const paths = event.paths;
        if (!paths.length) return;
        setUploading(true);
        let uploaded = 0;
        const failures: string[] = [];
        try {
          for (const path of paths) {
            try { await documentsApi.importPath(path); uploaded += 1; }
            catch (error) { failures.push(error instanceof Error ? error.message : `Could not import ${path}`); }
          }
          await refresh();
          if (uploaded) onSuccess(`${uploaded} document${uploaded === 1 ? "" : "s"} imported from drop.`);
          if (failures.length) onError(failures.join("\\n"));
        } finally { if (!cancelled) setUploading(false); }
      }
    }).then(fn => { if (cancelled) fn(); else unlisten = fn; });
    return () => { cancelled = true; unlisten?.(); };
  }, [onError, onSuccess, refresh]);

  async function uploadFiles(files: FileList | null) {
    if (!files?.length) return;
    setUploading(true);
    let uploaded = 0;
    try {
      for (const file of Array.from(files)) {
        const extension = file.name.split(".").pop()?.toLowerCase();
        if (!["pdf", "docx", "txt"].includes(extension || "")) {
          onError(`${file.name}: only PDF, DOCX and TXT files are supported.`); continue;
        }
        if (file.size > MAX_BYTES) { onError(`${file.name}: maximum file size is 50 MB.`); continue; }
        await documentsApi.upload(file);
        uploaded += 1;
      }
      await refresh();
      if (uploaded) onSuccess(`${uploaded} document${uploaded === 1 ? "" : "s"} uploaded successfully.`);
    } catch (error) { onError(error instanceof Error ? error.message : "Document upload failed"); }
    finally { setUploading(false); if (inputRef.current) inputRef.current.value = ""; }
  }

  async function removeDocument(document: LocalDocument) {
    if (!window.confirm(`Delete "${document.filename}" from LOCAL AI STUDIO?`)) return;
    try { await documentsApi.delete(document.id); await refresh(); onSuccess("Document deleted."); }
    catch (error) { onError(error instanceof Error ? error.message : "Unable to delete document"); }
  }

  async function runOcr(document: LocalDocument) {
    setOcrRunning(document.id);
    try {
      const result = await documentsApi.runOcr(document.id);
      await refresh();
      onSuccess(`OCR completed for ${document.filename}. ${result.pages_processed} page(s) processed.`);
      const text = await documentsApi.getText(document.id);
      setPreview({ ...text, pdfUrl: undefined });
    } catch (error) {
      onError(error instanceof Error ? error.message : "OCR could not be completed");
    } finally { setOcrRunning(null); }
  }

  const selectedStudyDocument = documents.find(item => item.id === studyDocumentId) || null;
  useEffect(() => {
    if (!studyDocumentId && documents.length) setStudyDocumentId(documents[0].id);
  }, [documents, studyDocumentId]);
  useEffect(() => {
    if (!studyDocumentId) { setSavedMaterials([]); return; }
    documentStudyApi.materials(studyDocumentId).then(setSavedMaterials).catch(error => onError(error instanceof Error ? error.message : "Unable to load saved study materials"));
  }, [studyDocumentId, onError]);

  async function runStudy(action: () => Promise<void>) {
    setStudyBusy(true);
    setStudyError("");
    try { await action(); }
    catch (error) {
      const message = error instanceof Error ? error.message : "Study operation failed";
      setStudyError(message);
      onError(message);
    }
    finally { setStudyBusy(false); }
  }

  async function autoSaveMaterial(type: string, title: string, content: string) {
    await documentStudyApi.saveMaterial(studyDocumentId, type, title, content);
    setSavedMaterials(await documentStudyApi.materials(studyDocumentId));
  }
  async function askDocument() {
    if (!studyDocumentId || !studyQuestion.trim()) return;
    await runStudy(async () => { const result = await documentStudyApi.ask(studyDocumentId, studyQuestion.trim()); setStudyAnswer(result); await autoSaveMaterial("answer", `Q&A: ${studyQuestion.trim().slice(0, 70)}`, JSON.stringify({ question: studyQuestion.trim(), ...result }, null, 2)); });
  }
  async function makeSynopsis() {
    if (!studyDocumentId) return;
    await runStudy(async () => { const result = await documentStudyApi.synopsis(studyDocumentId, synopsisStyle); setSynopsis(result.synopsis); await autoSaveMaterial("synopsis", `Synopsis (${synopsisStyle})`, result.synopsis); });
  }
  async function makeQuestions() {
    if (!studyDocumentId) return;
    await runStudy(async () => { const result = await documentStudyApi.questions(studyDocumentId, questionCount, difficulty, questionType); setStudyQuestions(result.questions); setRevealedAnswers([]); setQuizAnswers({}); setQuizSubmitted(false); await autoSaveMaterial("questions", `Questions (${result.questions.length})`, JSON.stringify(result.questions, null, 2)); });
  }
  async function makeFlashcards() {
    if (!studyDocumentId) return;
    await runStudy(async () => { const result = await documentStudyApi.flashcards(studyDocumentId, questionCount); setFlashcards(result.flashcards); setFlashIndex(0); setFlashRevealed(false); await autoSaveMaterial("flashcards", `Flashcards (${result.flashcards.length})`, JSON.stringify(result.flashcards, null, 2)); });
  }
  async function makeGlossary() {
    if (!studyDocumentId) return;
    await runStudy(async () => { const result = await documentStudyApi.glossary(studyDocumentId); setGlossary(result.glossary); await autoSaveMaterial("glossary", `Glossary (${result.glossary.length})`, JSON.stringify(result.glossary, null, 2)); });
  }
  async function makeConceptMap() {
    if (!studyDocumentId) return;
    await runStudy(async () => { const result = await documentStudyApi.conceptMap(studyDocumentId); setConceptMap(result.concept_map); await autoSaveMaterial("concept_map", "Document concept map", result.concept_map); });
  }
  async function saveStudyMaterial(type: string, title: string, content: string) {
    if (!studyDocumentId || !content.trim()) return;
    await runStudy(async () => { await documentStudyApi.saveMaterial(studyDocumentId, type, title, content); setSavedMaterials(await documentStudyApi.materials(studyDocumentId)); onSuccess("Study material saved locally."); });
  }
  const filtered = documents.filter(item => item.filename.toLowerCase().includes(query.toLowerCase()));
  return <main className="documents-workspace">
    <header className="documents-header">
      <div><div className="documents-eyebrow">WORKSPACE</div><h1>Documents</h1><p>Store, view, extract and OCR your documents locally. Scanned PDFs can be processed using your local Tesseract installation.</p></div>
      <div className="documents-header-actions">
        <button className="documents-secondary" onClick={() => void refresh()} disabled={loading}><RefreshCw size={16}/> Refresh</button>
        <button className="documents-primary" onClick={() => inputRef.current?.click()} disabled={uploading}><FileUp size={17}/>{uploading ? "Uploading…" : "Upload documents"}</button>
        <input ref={inputRef} type="file" accept=".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain" multiple hidden onChange={event => void uploadFiles(event.target.files)}/>
      </div>
    </header>
    <section className={`documents-dropzone${dragActive ? " is-drag-active" : ""}`} onDragEnter={event => { event.preventDefault(); setDragActive(true); }} onDragOver={event => event.preventDefault()} onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragActive(false); }} onDrop={event => { event.preventDefault(); setDragActive(false); void uploadFiles(event.dataTransfer.files); }}>
      <div className="documents-upload-icon"><FileUp size={25}/></div>
      <strong>{dragActive ? "Release to import documents" : "Drop documents here"}</strong><span>or use Upload documents to browse your computer</span>
      <small>Supported formats: PDF, DOCX, TXT · Maximum 50 MB per file</small>
    </section>
    <div className="documents-list-heading"><div><h2>Your documents</h2><span>{documents.length} stored</span></div><label className="documents-search"><Search size={15}/><input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search documents…"/></label></div>
    {loading ? <div className="documents-empty">Loading documents…</div> : filtered.length === 0 ? <div className="documents-empty"><FileText size={30}/><strong>{query ? "No matching documents" : "No documents yet"}</strong><span>Upload a PDF, Word document or text file to get started.</span></div> :
      <div className="documents-table">{filtered.map(document => <article className="document-row" key={document.id}>
        <div className="document-file-icon"><FileIcon size={21}/></div><div className="document-file-info"><strong title={document.filename}>{document.filename}</strong><span>{document.file_type} · {formatSize(document.size_bytes)}</span></div>
        <div className={`document-status status-${document.status}`}><span className="document-status-dot"/>{document.status || "Uploaded"}</div>{document.file_type.toUpperCase() === "PDF" && <button className="documents-view-text" onClick={async () => { try { const blob = await documentsApi.getFile(document.id); const pdfUrl = URL.createObjectURL(new Blob([blob], { type: "application/pdf" })); setPreview({ filename: document.filename, text: "", status: document.status, page_count: document.page_count ?? null, pdfUrl }); } catch (error) { onError(error instanceof Error ? error.message : "Could not open PDF"); } }}>View PDF</button>}{document.file_type.toUpperCase() === "PDF" && (document.status === "needs_ocr" || document.status === "ocr_failed") && <button className="documents-ocr-button" disabled={ocrRunning === document.id} onClick={() => void runOcr(document)}>{ocrRunning === document.id ? <Loader2 size={14} className="spin"/> : <ScanText size={14}/>} {ocrRunning === document.id ? "Running OCR…" : "Run OCR"}</button>}<button className="documents-view-text" onClick={async () => { try { const result = await documentsApi.getText(document.id); setPreview({ ...result, pdfUrl: undefined }); } catch (error) { onError(error instanceof Error ? error.message : "Could not load extracted text"); } }}>View text</button><div className="document-date">{formatDate(document.uploaded_at)}</div>
        <button className="document-delete" title="Delete document" onClick={() => void removeDocument(document)}><Trash2 size={16}/></button>
      </article>)}</div>}
    {preview && <div className="document-preview-backdrop" onClick={() => setPreview(null)}><section className="document-preview" onClick={event => event.stopPropagation()}><header><div><strong>{preview.filename}</strong><small>{preview.status}{preview.page_count ? ` · ${preview.page_count} pages` : ""}</small></div><button onClick={() => setPreview(null)}>Close</button></header>{preview.pdfUrl ? <iframe className="document-pdf-frame" src={preview.pdfUrl} title={`PDF preview: ${preview.filename}`} /> : <pre>{preview.text || "No extracted text is available yet. Use Run OCR for scanned PDFs. The original PDF remains available using View PDF."}</pre>}</section></div>}
    <section className="study-panel">
      <div className="study-panel-heading"><div><div className="documents-eyebrow">LOCAL DOCUMENT INTELLIGENCE</div><h2>Study with your documents</h2><p>Ask questions, create summaries and generate revision materials using the selected document and your local model.</p></div>
        <select value={studyDocumentId} onChange={event => { setStudyDocumentId(event.target.value); setStudyAnswer(null); setSynopsis(""); setStudyQuestions([]); setFlashcards([]); setGlossary([]); setConceptMap(""); }} disabled={!documents.length}><option value="">Select a document</option>{documents.map(doc => <option key={doc.id} value={doc.id}>{doc.filename}</option>)}</select>
      </div>
      {studyError && <div className="study-error" role="alert"><strong>Study generation failed</strong><p>{studyError}</p><button className="documents-secondary" onClick={() => setStudyError("")}>Dismiss</button></div>}
      {!selectedStudyDocument ? <div className="study-empty">Upload a document to start asking questions and generating study materials.</div> : !selectedStudyDocument.extracted_chars ? <div className="study-empty">This document has no extracted text yet. Run OCR or text extraction before using AI study tools.</div> : <>
        <nav className="study-tabs">{([ ["ask","Ask AI"],["synopsis","Synopsis"],["questions","Questions"],["quiz","Quiz"],["flashcards","Flashcards"],["glossary","Glossary"],["concept","Concept map"],["notes","Notes"] ] as const).map(([key,label]) => <button key={key} className={studyTab===key?"active":""} onClick={()=>setStudyTab(key)}>{label}</button>)}</nav>
        {studyTab === "ask" && <div className="study-content"><h3>Ask anything about this document</h3><textarea value={studyQuestion} onChange={event=>setStudyQuestion(event.target.value)} placeholder="Example: Explain the main concept in simple words…" rows={3}/><button className="documents-primary" disabled={studyBusy || !studyQuestion.trim()} onClick={()=>void askDocument()}>{studyBusy?"Thinking…":"Ask document"}</button>{studyAnswer && <article className="study-result"><div className="study-result-label">{studyAnswer.grounded?"Answer based on retrieved document passages":"No supporting passage found"}</div><pre>{studyAnswer.answer}</pre>{studyAnswer.sources.length>0 && <small>Retrieved pages: {studyAnswer.sources.join(", ")}</small>}</article>}</div>}
        {studyTab === "synopsis" && <div className="study-content"><h3>Generate a synopsis</h3><div className="study-controls"><select value={synopsisStyle} onChange={e=>setSynopsisStyle(e.target.value as typeof synopsisStyle)}><option value="short">Short synopsis</option><option value="detailed">Detailed synopsis</option><option value="chapter">Chapter-wise</option><option value="topic">Topic-wise</option></select><button className="documents-primary" disabled={studyBusy} onClick={()=>void makeSynopsis()}>{studyBusy?"Generating…":"Generate synopsis"}</button></div>{synopsis && <><article className="study-result"><pre>{synopsis}</pre></article><button className="documents-secondary" onClick={()=>void saveStudyMaterial("note","Synopsis",synopsis)}>Save synopsis</button></>}</div>}
        {studyTab === "questions" && <div className="study-content"><h3>Generate questions from the document</h3><div className="study-controls"><label>Count <select value={questionCount} onChange={e=>setQuestionCount(Number(e.target.value))}><option>5</option><option>10</option><option>15</option><option>20</option><option>30</option></select></label><label>Difficulty <select value={difficulty} onChange={e=>setDifficulty(e.target.value)}><option value="mixed">Mixed</option><option value="easy">Easy</option><option value="medium">Medium</option><option value="hard">Hard</option></select></label><label>Type <select value={questionType} onChange={e=>setQuestionType(e.target.value)}><option value="mixed">Mixed</option><option value="mcq">MCQ</option><option value="short">Short answer</option><option value="long">Long answer</option></select></label><button className="documents-primary" disabled={studyBusy} onClick={()=>void makeQuestions()}>{studyBusy?"Generating…":"Generate questions"}</button></div>{studyQuestions.map((q,i)=><article className="study-question" key={i}><strong>{i+1}. {q.question}</strong><small>{q.difficulty} · {q.type}{q.page?` · Page ${q.page}`:""}</small>{q.options?.length>0 && <ul>{q.options.map((o,j)=><li key={j}>{o}</li>)}</ul>}{revealedAnswers.includes(i)?<div className="study-answer"><b>Answer:</b> {q.answer}<p>{q.explanation}</p></div>:<button className="documents-secondary" onClick={()=>setRevealedAnswers(prev=>[...prev,i])}>Show answer</button>}</article>)}{studyQuestions.length>0 && <button className="documents-secondary" onClick={()=>void saveStudyMaterial("questions","Generated questions",JSON.stringify(studyQuestions,null,2))}>Save question set</button>}</div>}
        {studyTab === "quiz" && <div className="study-content"><h3>Quiz mode</h3>{!studyQuestions.length ? <p>Generate a question set in the Questions tab first, then return here to take the quiz.</p> : <><p>Choose your answers, then submit to see your score and explanations.</p>{studyQuestions.map((q,i)=><article className="study-question" key={i}><strong>{i+1}. {q.question}</strong>{q.options?.length ? q.options.map((option,j)=><label className="study-option" key={j}><input type="radio" name={`quiz-${i}`} checked={quizAnswers[i]===option} disabled={quizSubmitted} onChange={()=>setQuizAnswers(prev=>({...prev,[i]:option}))}/>{option}</label>) : <textarea rows={2} disabled={quizSubmitted} value={quizAnswers[i]||""} onChange={e=>setQuizAnswers(prev=>({...prev,[i]:e.target.value}))} placeholder="Your answer"/>}{quizSubmitted && <div className="study-answer"><b>Expected answer:</b> {q.answer}<p>{q.explanation}</p></div>}</article>)}{!quizSubmitted?<button className="documents-primary" onClick={()=>setQuizSubmitted(true)}>Submit quiz</button>:<div className="study-result"><b>Quiz submitted.</b><p>Review the expected answers above. MCQ scoring is based on exact answer matching.</p><strong>Score: {studyQuestions.filter((q,i)=>q.options?.length && quizAnswers[i]===q.answer).length} / {studyQuestions.filter(q=>q.options?.length).length} MCQs</strong><button className="documents-secondary" onClick={()=>{setQuizSubmitted(false);setQuizAnswers({});}}>Retry quiz</button></div>}</>}</div>}
        {studyTab === "flashcards" && <div className="study-content"><h3>Flashcards</h3><button className="documents-primary" disabled={studyBusy} onClick={()=>void makeFlashcards()}>{studyBusy?"Generating…":"Generate flashcards"}</button>{flashcards.length>0 && <div className="study-flashcard" onClick={()=>setFlashRevealed(v=>!v)}><small>{flashRevealed?"ANSWER":"QUESTION · CLICK TO FLIP"}</small><strong>{flashRevealed?flashcards[flashIndex]?.back:flashcards[flashIndex]?.front}</strong>{flashcards[flashIndex]?.page && <span>Page {flashcards[flashIndex].page}</span>}<div><button className="documents-secondary" onClick={e=>{e.stopPropagation();setFlashIndex(v=>Math.max(0,v-1));setFlashRevealed(false);}}>Previous</button><span>{flashIndex+1} / {flashcards.length}</span><button className="documents-secondary" onClick={e=>{e.stopPropagation();setFlashIndex(v=>Math.min(flashcards.length-1,v+1));setFlashRevealed(false);}}>Next</button></div></div>}{flashcards.length>0 && <button className="documents-secondary" onClick={()=>void saveStudyMaterial("flashcards","Generated flashcards",JSON.stringify(flashcards,null,2))}>Save flashcards</button>}</div>}
        {studyTab === "glossary" && <div className="study-content"><h3>Important terms and glossary</h3><button className="documents-primary" disabled={studyBusy} onClick={()=>void makeGlossary()}>{studyBusy?"Generating…":"Generate glossary"}</button>{glossary.map((item,i)=><article className="study-question" key={i}><strong>{item.term}</strong><p>{item.definition}</p>{item.page && <small>Page {item.page}</small>}</article>)}{glossary.length>0 && <button className="documents-secondary" onClick={()=>void saveStudyMaterial("glossary","Document glossary",JSON.stringify(glossary,null,2))}>Save glossary</button>}</div>}
        {studyTab === "concept" && <div className="study-content"><h3>Document concept map</h3><button className="documents-primary" disabled={studyBusy} onClick={()=>void makeConceptMap()}>{studyBusy?"Generating…":"Generate concept map"}</button>{conceptMap && <><article className="study-result"><pre>{conceptMap}</pre></article><p>Copy this Mermaid mindmap source into a Mermaid-compatible viewer to render the diagram.</p><button className="documents-secondary" onClick={()=>void saveStudyMaterial("concept_map","Document concept map",conceptMap)}>Save concept map</button></>}</div>}
        {studyTab === "notes" && <div className="study-content"><h3>My study notes</h3><input value={noteTitle} onChange={e=>setNoteTitle(e.target.value)} placeholder="Note title"/><textarea rows={6} value={noteContent} onChange={e=>setNoteContent(e.target.value)} placeholder="Write notes for this document…"/><button className="documents-primary" disabled={studyBusy || !noteContent.trim()} onClick={()=>void saveStudyMaterial("note",noteTitle||"Study note",noteContent)}>Save note</button><div className="saved-materials">{savedMaterials.map(material=><article className="study-question" key={material.id}><strong>{material.title || material.material_type}</strong><small>{material.material_type} · {new Date(material.created_at).toLocaleString()}</small><pre>{material.content}</pre><button className="documents-secondary" onClick={()=>void runStudy(async()=>{await documentStudyApi.deleteMaterial(studyDocumentId,material.id);setSavedMaterials(await documentStudyApi.materials(studyDocumentId));})}>Delete</button></article>)}</div></div>}
      </>}
      {selectedStudyDocument && savedMaterials.length > 0 && <section className="saved-materials"><h3>Saved study materials</h3>{savedMaterials.map(material=><article className="study-question" key={material.id}><strong>{material.title || material.material_type}</strong><small>{material.material_type} · {new Date(material.created_at).toLocaleString()}</small><pre>{material.content}</pre><button className="documents-secondary" onClick={() => void runStudy(async () => { await documentStudyApi.deleteMaterial(studyDocumentId, material.id); setSavedMaterials(await documentStudyApi.materials(studyDocumentId)); })}>Delete</button></article>)}</section>}
    </section>
    <footer className="documents-privacy"><ShieldCheck size={16}/><span>Files, extracted text and saved study materials remain in your local LOCAL AI STUDIO data directory. AI requests are sent only to your configured local llama.cpp server.</span></footer>
  </main>;
}
