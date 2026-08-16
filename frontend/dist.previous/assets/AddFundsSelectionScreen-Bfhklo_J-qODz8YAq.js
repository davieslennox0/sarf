import{e_ as x,d6 as y,d7 as C,db as i,e$ as g,d8 as r,dv as l}from"./index-rKSqu6XN.js";import{n as j}from"./styles-DVyDvTdj-Dwgb_fvQ.js";import{i as a,d as c,l as d,Q as b}from"./styles-CWoC81ZD-ROJjkJca.js";import{c as w}from"./createLucideIcon-CdCZMpN3.js";import{C as k}from"./credit-card-CgZQEIpF.js";import"./ScreenLayout-Dy-3vlz4-B_6ajx25.js";import"./ModalHeader-BS54PZSj-7FKhUGA_.js";import"./Screen-Xe9xaKl3-DbuZUFPF.js";import"./index-Dq_xe9dz-S-h1YqQw.js";/**
 * @license lucide-react v0.554.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const v=[["rect",{width:"20",height:"12",x:"2",y:"6",rx:"2",key:"9lu3g6"}],["circle",{cx:"12",cy:"12",r:"2",key:"1c9p78"}],["path",{d:"M6 12h.01M18 12h.01",key:"113zkx"}]],u=w("banknote",v),A={component:()=>{let e=x(),{onUserCloseViaDialogOrKeybindRef:s}=y(),p=C(),t=i.useRef(!1);i.useEffect(()=>{e&&(t.current=!1)},[e]);let o=i.useCallback(async()=>{!t.current&&e&&(t.current=!0,g(),await e.onCancel())},[e]);return i.useEffect(()=>(s.current=o,()=>{s.current===o&&(s.current=null)}),[o,s]),e?e.error?r.jsx(a,{icon:u,iconVariant:"warning",title:"Unable to add funds",subtitle:e.error,showClose:!0,onClose:o,primaryCta:{label:"Close",onClick:o}}):r.jsx(a,{icon:u,iconVariant:"subtle",title:"Select method",subtitle:"Choose how to fund your wallet",showClose:!0,onClose:o,children:r.jsxs(j,{style:{marginTop:"1rem"},$colorScheme:p.appearance.palette.colorScheme,children:[e.startFiat&&r.jsxs(c,{onClick:async()=>{var n;t.current||(t.current=!0,await((n=e.startFiat)==null?void 0:n.call(e)))},children:[r.jsx(h,{children:r.jsx(k,{})}),r.jsxs(m,{children:[r.jsx(d,{children:"Pay with fiat"}),r.jsx(f,{children:"Apple Pay, Google Pay, or debit card"})]})]}),e.startCrypto&&r.jsxs(c,{onClick:async()=>{var n;t.current||(t.current=!0,await((n=e.startCrypto)==null?void 0:n.call(e)))},children:[r.jsx(h,{children:r.jsx(b,{})}),r.jsxs(m,{children:[r.jsx(d,{children:"Transfer from wallet"}),r.jsx(f,{children:"Send crypto from any wallet"})]})]})]})}):null}};let h=l.span`
  width: 2rem;
  height: 2rem;
  border-radius: var(--privy-border-radius-full);
  background-color: var(--privy-color-background-2);
  color: var(--color-icon-muted, #64668b);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;

  svg {
    width: 1.125rem;
    height: 1.125rem;
  }
`,m=l.span`
  display: flex;
  flex-direction: column;
  align-items: flex-start;
`,f=l.span`
  font-size: 0.875rem;
  line-height: 1.25rem;
  color: var(--privy-color-foreground-3);
`;export{A as AddFundsSelectionScreen,A as default};
