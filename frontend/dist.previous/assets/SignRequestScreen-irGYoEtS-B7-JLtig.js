import{dD as A,d9 as N,d6 as M,db as o,fj as z,dJ as b,eP as E,eQ as C,d8 as a,dz as O,dv as p,cj as I,cg as P,fk as q}from"./index-CcqrMd20.js";import{h as F}from"./CopyToClipboard-DSTf_eKU-D6gRxYgM.js";import{a as J}from"./Layouts-BlFm53ED-wZSlhVgB.js";import{a as V,i as H}from"./JsonTree-aPaJmPx7-S3oeby8s.js";import{n as Q}from"./ScreenLayout-Dy-3vlz4-CHF3YhgI.js";import{c as K}from"./createLucideIcon-Bak-CNgg.js";import"./ModalHeader-BS54PZSj-1PZGPBIa.js";import"./Screen-Xe9xaKl3-BhaONPgj.js";import"./index-Dq_xe9dz-yRI978md.js";/**
 * @license lucide-react v0.554.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const W=[["path",{d:"M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7",key:"1m0v6g"}],["path",{d:"M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z",key:"ohrbg2"}]],B=K("square-pen",W),G=p.img`
  && {
    height: ${e=>e.size==="sm"?"65px":"140px"};
    width: ${e=>e.size==="sm"?"65px":"140px"};
    border-radius: 16px;
    margin-bottom: 12px;
  }
`;let X=e=>{if(!I(e))return e;try{let s=P(e);return s.includes("�")?e:s}catch{return e}},Y=e=>{try{let s=q.decode(e),i=new TextDecoder().decode(s);return i.includes("�")?e:i}catch{return e}},Z=e=>{let{types:s,primaryType:i,...l}=e.typedData;return a.jsxs(a.Fragment,{children:[a.jsx(te,{data:l}),a.jsx(F,{text:(r=e.typedData,JSON.stringify(r,null,2)),itemName:"full payload to clipboard"})," "]});var r};const k=({method:e,messageData:s,copy:i,iconUrl:l,isLoading:r,success:g,walletProxyIsLoading:m,errorMessage:x,isCancellable:d,onSign:c,onCancel:y,onClose:u})=>a.jsx(Q,{title:i.title,subtitle:i.description,showClose:!0,onClose:u,icon:B,iconVariant:"subtle",helpText:x?a.jsx(ee,{children:x}):void 0,primaryCta:{label:i.buttonText,onClick:c,disabled:r||g||m,loading:r},secondaryCta:d?{label:"Not now",onClick:y,disabled:r||g||m}:void 0,watermark:!0,children:a.jsxs(J,{children:[l?a.jsx(G,{style:{alignSelf:"center"},size:"sm",src:l,alt:"app image"}):null,a.jsxs($,{children:[e==="personal_sign"&&a.jsx(w,{children:X(s)}),e==="eth_signTypedData_v4"&&a.jsx(Z,{typedData:s}),e==="solana_signMessage"&&a.jsx(w,{children:Y(s)})]})]})}),ue={component:()=>{let{authenticated:e}=A(),{initializeWalletProxy:s,closePrivyModal:i}=N(),{navigate:l,data:r,onUserCloseViaDialogOrKeybindRef:g}=M(),[m,x]=o.useState(!0),[d,c]=o.useState(""),[y,u]=o.useState(),[f,T]=o.useState(null),[j,S]=o.useState(!1);o.useEffect(()=>{e||l("LandingScreen")},[e]),o.useEffect(()=>{s(z).then(n=>{x(!1),n||(c("An error has occurred, please try again."),u(new b(new E(d,C.E32603_DEFAULT_INTERNAL_ERROR.eipCode))))})},[]);let{method:_,data:R,confirmAndSign:v,onSuccess:D,onFailure:L,uiOptions:t}=r.signMessage,U={title:(t==null?void 0:t.title)||"Sign message",description:(t==null?void 0:t.description)||"Signing this message will not cost you any fees.",buttonText:(t==null?void 0:t.buttonText)||"Sign and continue"},h=n=>{n?D(n):L(y||new b(new E("The user rejected the request.",C.E4001_USER_REJECTED_REQUEST.eipCode))),i({shouldCallAuthOnSuccess:!1}),setTimeout(()=>{T(null),c(""),u(void 0)},200)};return g.current=()=>{h(f)},a.jsx(k,{method:_,messageData:R,copy:U,iconUrl:t!=null&&t.iconUrl&&typeof t.iconUrl=="string"?t.iconUrl:void 0,isLoading:j,success:f!==null,walletProxyIsLoading:m,errorMessage:d,isCancellable:t==null?void 0:t.isCancellable,onSign:async()=>{S(!0),c("");try{let n=await v();T(n),S(!1),setTimeout(()=>{h(n)},O)}catch(n){console.error(n),c("An error has occurred, please try again."),u(new b(new E(d,C.E32603_DEFAULT_INTERNAL_ERROR.eipCode))),S(!1)}},onCancel:()=>h(null),onClose:()=>h(f)})}};let $=p.div`
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 16px;
`,ee=p.p`
  && {
    margin: 0;
    width: 100%;
    text-align: center;
    color: var(--privy-color-error-dark);
    font-size: 14px;
    line-height: 22px;
  }
`,te=p(V)`
  margin-top: 0;
`,w=p(H)`
  margin-top: 0;
`;export{ue as SignRequestScreen,k as SignRequestView,ue as default};
