import {useContext,useState} from 'react';
import {Link} from 'react-router-dom';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {Context,useData} from './App';
export function useAction(){const ctx=useContext(Context);const [busy,setBusy]=useState(false);async function act(fn:()=>Promise<any>,message='Saved'){setBusy(true);try{const result=await fn();ctx.notify(message);ctx.refresh();return result}catch(e:any){ctx.notify(e.message,true);return null}finally{setBusy(false)}}return {act,busy,...ctx};}
export function Prose({text}:{text:string}){const {data:pages}=useData('/wiki');const mapped=text.replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g,(_,name,label)=>{const page=pages?.find((p:any)=>p.title.toLowerCase()===name.toLowerCase()||JSON.parse(p.doc).aliases.some((a:string)=>a.toLowerCase()===name.toLowerCase()));return page?`[${label||name}](/wiki/${page.id})`:`${label||name} (unresolved link)`});return <Markdown remarkPlugins={[remarkGfm]} components={{img:({alt})=><span className="muted">[Image: {alt||'external image not loaded'}]</span>,a:({href,children})=>href?.startsWith('/')?<Link to={href}>{children}</Link>:<a href={href} target="_blank" rel="noreferrer">{children}</a>}}>{mapped}</Markdown>}

