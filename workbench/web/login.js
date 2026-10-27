'use strict';
const form=document.querySelector('#login'),message=document.querySelector('#message');
form.onsubmit=async e=>{e.preventDefault();const button=form.querySelector('button');button.disabled=true;message.textContent='';try{
 const response=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json','X-Local-App':'1'},body:JSON.stringify(Object.fromEntries(new FormData(form)))});
 const data=await response.json();if(!response.ok)throw Error(data.error||'登录失败');
 const next=new URLSearchParams(location.search).get('next')||'/workbench/';
 // Reject scheme-relative URLs, backslashes and controls before navigation.
 location.replace(next.startsWith('/')&&!next.startsWith('//')&&!/[\\\u0000-\u0020]/.test(next)?next:'/workbench/');
}catch(error){message.textContent=error.message;button.disabled=false}};
