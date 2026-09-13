export async function api(path:string,method='GET',body?:unknown,headers:Record<string,string>={}):Promise<any>{
 const form=body instanceof FormData;
 const response=await fetch('/api'+path,{method,headers:{...headers,'X-Commonplace':'local',...(!form&&body!==undefined?{'Content-Type':'application/json'}:{})},body:body===undefined?undefined:form?body:JSON.stringify(body)});
 const data=await response.json();
 if(!response.ok)throw new Error(data.error||data.detail?.[0]?.msg||'Request failed');
 return data;
}
export const date=(s:string)=>new Date(s).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});

