import{db as s,d6 as re,d7 as Q,d9 as oe,dD as ae,dz as Z,d8 as r,dP as E,dd as b,dM as te,dB as ne,dv as C}from"./index-rKSqu6XN.js";import{F as ie}from"./EnvelopeIcon-Dm4iO2WC.js";import{F as se}from"./PhoneIcon-Cc2El2K4.js";import{o as le}from"./Layouts-BlFm53ED-BAgAvBp8.js";import{n as ce}from"./Link-DJ5gq9Di-B6LhkhYS.js";import{a as de}from"./shouldProceedtoEmbeddedWalletCreationFlow-Zce_uO5W-BvR7XfEW.js";import{n as ue}from"./ScreenLayout-Dy-3vlz4-B_6ajx25.js";import"./ModalHeader-BS54PZSj-7FKhUGA_.js";import"./Screen-Xe9xaKl3-DbuZUFPF.js";import"./index-Dq_xe9dz-S-h1YqQw.js";function pe({title:o,titleId:p,...T},m){return s.createElement("svg",Object.assign({xmlns:"http://www.w3.org/2000/svg",viewBox:"0 0 20 20",fill:"currentColor","aria-hidden":"true","data-slot":"icon",ref:m,"aria-labelledby":p},T),o?s.createElement("title",{id:p},o):null,s.createElement("path",{fillRule:"evenodd",d:"M16.704 4.153a.75.75 0 0 1 .143 1.052l-8 10.5a.75.75 0 0 1-1.127.075l-4.5-4.5a.75.75 0 0 1 1.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 0 1 1.05-.143Z",clipRule:"evenodd"}))}const me=s.forwardRef(pe),fe=({contactMethod:o,authFlow:p,emailDomain:T,appName:m="Privy",whatsAppEnabled:j=!1,onBack:l,onCodeSubmit:_,onResend:M,errorMessage:f,success:g=!1,resendCountdown:D=0,onInvalidInput:N,onClearError:v})=>{let[d,A]=s.useState(J);s.useEffect(()=>{f||A(J)},[f]);let w=async y=>{var x;y.preventDefault();let n=y.currentTarget.value.replace(" ","");if(n==="")return;if(isNaN(Number(n)))return void(N==null?void 0:N("Code should be numeric"));v==null||v();let h=Number((x=y.currentTarget.name)==null?void 0:x.charAt(5)),c=[...n||[""]].slice(0,G-h),t=[...d.slice(0,h),...c,...d.slice(h+c.length)];A(t);let S=Math.min(Math.max(h+c.length,0),G-1);if(!isNaN(Number(y.currentTarget.value))){let a=document.querySelector(`input[name=code-${S}]`);a==null||a.focus()}if(t.every(a=>a&&!isNaN(+a))){let a=document.querySelector(`input[name=code-${S}]`);a==null||a.blur(),await(_==null?void 0:_(t.join("")))}};return r.jsx(ue,{title:"Enter confirmation code",subtitle:r.jsxs("span",p==="email"?{children:["Please check ",r.jsx(ee,{children:o})," for an email from"," ",T??"privy.io"," and enter your code below."]}:{children:["Please check ",r.jsx(ee,{children:o})," for a",j?" WhatsApp":""," message from ",m," and enter your code below."]}),icon:p==="email"?ie:se,onBack:l,showBack:!0,helpText:r.jsxs(Ee,{children:[r.jsxs("span",{children:["Didn't get ",p==="email"?"an email":"a message","?"]}),D?r.jsxs(be,{children:[r.jsx(me,{color:"var(--privy-color-foreground)",strokeWidth:1.33,height:"12px",width:"12px"}),r.jsx("span",{children:"Code sent"})]}):r.jsx(ce,{as:"button",size:"sm",onClick:M,children:"Resend code"})]}),children:r.jsx(xe,{children:r.jsx(le,{children:r.jsxs(ge,{children:[r.jsx("div",{children:d.map((y,n)=>r.jsx("input",{name:`code-${n}`,type:"text",value:d[n],onChange:w,onKeyUp:h=>{h.key==="Backspace"&&(c=>{if(v==null||v(),A([...d.slice(0,c),"",...d.slice(c+1)]),c>0){let t=document.querySelector(`input[name=code-${c-1}]`);t==null||t.focus()}})(n)},inputMode:"numeric",autoFocus:n===0,pattern:"[0-9]",className:`${g?"success":""} ${f?"fail":""}`,autoComplete:ne?"one-time-code":"off"},n))}),r.jsx(ye,{$fail:!!f,$success:g,children:r.jsx("span",{children:f==="Invalid or expired verification code"?"Incorrect code":f||(g?"Success!":"")})})]})})})})};let G=6,J=Array(6).fill("");var k,R,ve=((k=ve||{})[k.RESET_AFTER_DELAY=0]="RESET_AFTER_DELAY",k[k.CLEAR_ON_NEXT_VALID_INPUT=1]="CLEAR_ON_NEXT_VALID_INPUT",k),he=((R=he||{})[R.EMAIL=0]="EMAIL",R[R.SMS=1]="SMS",R);const je={component:()=>{var F,P,U;let{navigate:o,lastScreen:p,navigateBack:T,setModalData:m,onUserCloseViaDialogOrKeybindRef:j}=re(),l=Q(),{closePrivyModal:_,resendEmailCode:M,resendSmsCode:f,getAuthMeta:g,loginWithCode:D,updateWallets:N,createAnalyticsEvent:v}=oe(),{authenticated:d,logout:A,user:w}=ae(),{whatsAppEnabled:y}=Q(),[n,h]=s.useState(!1),[c,t]=s.useState(null),[S,x]=s.useState(null),[a,L]=s.useState(0);j.current=()=>null;let I=(F=g())!=null&&F.email?0:1,$=I===0?((P=g())==null?void 0:P.email)||"":((U=g())==null?void 0:U.phoneNumber)||"",O=Z-500;return s.useEffect(()=>{if(a){let i=setTimeout(()=>{L(a-1)},1e3);return()=>clearTimeout(i)}},[a]),s.useEffect(()=>{if(d&&n&&w){if(l!=null&&l.legal.requireUsersAcceptTerms&&!w.hasAcceptedTerms){let i=setTimeout(()=>{o("AffirmativeConsentScreen")},O);return()=>clearTimeout(i)}if(de(w,l.embeddedWallets)){let i=setTimeout(()=>{m({createWallet:{onSuccess:()=>{},onFailure:u=>{console.error(u),v({eventName:"embedded_wallet_creation_failure_logout",payload:{error:u,screen:"AwaitingPasswordlessCodeScreen"}}),A()},callAuthOnSuccessOnClose:!0}}),o("EmbeddedWalletOnAccountCreateScreen")},O);return()=>clearTimeout(i)}{N();let i=setTimeout(()=>_({shouldCallAuthOnSuccess:!0,isSuccess:!0}),Z);return()=>clearTimeout(i)}}},[d,n,w]),s.useEffect(()=>{if(c&&S===0){let i=setTimeout(()=>{t(null),x(null);let u=document.querySelector("input[name=code-0]");u==null||u.focus()},1400);return()=>clearTimeout(i)}},[c,S]),r.jsx(fe,{contactMethod:$,authFlow:I===0?"email":"sms",emailDomain:l==null?void 0:l.appearance.emailDomain,appName:l==null?void 0:l.name,whatsAppEnabled:y,onBack:()=>T(),onCodeSubmit:async i=>{var u,W,B,q,V,z,K,X,Y,H;try{await D(i),h(!0)}catch(e){if(e instanceof E&&e.privyErrorCode===b.INVALID_CREDENTIALS)t("Invalid or expired verification code"),x(0);else if(e instanceof E&&e.privyErrorCode===b.CANNOT_LINK_MORE_OF_TYPE)t(e.message);else{if(e instanceof E&&e.privyErrorCode===b.USER_LIMIT_REACHED)return console.error(new te(e).toString()),void o("UserLimitReachedScreen");if(e instanceof E&&e.privyErrorCode===b.USER_DOES_NOT_EXIST)return void o("AccountNotFoundScreen");if(e instanceof E&&e.privyErrorCode===b.LINKED_TO_ANOTHER_USER)return m({errorModalData:{error:e,previousScreen:p??"AwaitingPasswordlessCodeScreen"}}),void o("ErrorScreen",!1);if(e instanceof E&&e.privyErrorCode===b.DISALLOWED_PLUS_EMAIL)return m({inlineError:{error:e}}),void o("ConnectOrCreateScreen",!1);if(e instanceof E&&e.privyErrorCode===b.ACCOUNT_TRANSFER_REQUIRED&&((W=(u=e.data)==null?void 0:u.data)!=null&&W.nonce))return m({accountTransfer:{nonce:(q=(B=e.data)==null?void 0:B.data)==null?void 0:q.nonce,account:$,displayName:(K=(z=(V=e.data)==null?void 0:V.data)==null?void 0:z.account)==null?void 0:K.displayName,linkMethod:I===0?"email":"sms",embeddedWalletAddress:(H=(Y=(X=e.data)==null?void 0:X.data)==null?void 0:Y.otherUser)==null?void 0:H.embeddedWalletAddress}}),void o("LinkConflictScreen");t("Issue verifying code"),x(0)}}},onResend:async()=>{L(30),I===0?await M():await f()},errorMessage:c||void 0,success:n,resendCountdown:a,onInvalidInput:i=>{t(i),x(1)},onClearError:()=>{S===1&&(t(null),x(null))}})}};let xe=C.div`
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  margin: auto;
  gap: 16px;
  flex-grow: 1;
  width: 100%;
`,ge=C.div`
  display: flex;
  flex-direction: column;
  width: 100%;
  gap: 12px;

  > div:first-child {
    display: flex;
    justify-content: center;
    gap: 0.5rem;
    width: 100%;
    border-radius: var(--privy-border-radius-sm);

    > input {
      border: 1px solid var(--privy-color-foreground-4);
      background: var(--privy-color-background);
      border-radius: var(--privy-border-radius-sm);
      padding: 8px 10px;
      height: 48px;
      width: 40px;
      text-align: center;
      font-size: 18px;
      font-weight: 600;
      color: var(--privy-color-foreground);
      transition: all 0.2s ease;
    }

    > input:focus {
      border: 1px solid var(--privy-color-foreground);
      box-shadow: 0 0 0 1px var(--privy-color-foreground);
    }

    > input:invalid {
      border: 1px solid var(--privy-color-error);
    }

    > input.success {
      border: 1px solid var(--privy-color-border-success);
      background: var(--privy-color-success-bg);
    }

    > input.fail {
      border: 1px solid var(--privy-color-border-error);
      background: var(--privy-color-error-bg);
      animation: shake 180ms;
      animation-iteration-count: 2;
    }
  }

  @keyframes shake {
    0% {
      transform: translate(1px, 0px);
    }
    33% {
      transform: translate(-1px, 0px);
    }
    67% {
      transform: translate(-1px, 0px);
    }
    100% {
      transform: translate(1px, 0px);
    }
  }
`,ye=C.div`
  line-height: 20px;
  min-height: 20px;
  font-size: 14px;
  font-weight: 400;
  color: ${o=>o.$success?"var(--privy-color-success-dark)":o.$fail?"var(--privy-color-error-dark)":"transparent"};
  display: flex;
  justify-content: center;
  width: 100%;
  text-align: center;
`,Ee=C.div`
  display: flex;
  gap: 8px;
  align-items: center;
  justify-content: center;
  width: 100%;
  color: var(--privy-color-foreground-2);
`,be=C.div`
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--privy-border-radius-sm);
  padding: 2px 8px;
  gap: 4px;
  background: var(--privy-color-background-2);
  color: var(--privy-color-foreground-2);
`,ee=C.span`
  font-weight: 500;
  word-break: break-all;
  color: var(--privy-color-foreground);
`;export{je as AwaitingPasswordlessCodeScreen,fe as AwaitingPasswordlessCodeScreenView,je as default};
