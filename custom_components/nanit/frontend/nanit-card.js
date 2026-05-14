function t(t,e,i,r){var s,n=arguments.length,a=n<3?e:null===r?r=Object.getOwnPropertyDescriptor(e,i):r;if("object"==typeof Reflect&&"function"==typeof Reflect.decorate)a=Reflect.decorate(t,e,i,r);else for(var o=t.length-1;o>=0;o--)(s=t[o])&&(a=(n<3?s(a):n>3?s(e,i,a):s(e,i))||a);return n>3&&a&&Object.defineProperty(e,i,a),a}"function"==typeof SuppressedError&&SuppressedError;const e=globalThis,i=e.ShadowRoot&&(void 0===e.ShadyCSS||e.ShadyCSS.nativeShadow)&&"adoptedStyleSheets"in Document.prototype&&"replace"in CSSStyleSheet.prototype,r=Symbol(),s=new WeakMap;let n=class{constructor(t,e,i){if(this._$cssResult$=!0,i!==r)throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");this.cssText=t,this.t=e}get styleSheet(){let t=this.o;const e=this.t;if(i&&void 0===t){const i=void 0!==e&&1===e.length;i&&(t=s.get(e)),void 0===t&&((this.o=t=new CSSStyleSheet).replaceSync(this.cssText),i&&s.set(e,t))}return t}toString(){return this.cssText}};const a=(t,...e)=>{const i=1===t.length?t[0]:e.reduce((e,i,r)=>e+(t=>{if(!0===t._$cssResult$)return t.cssText;if("number"==typeof t)return t;throw Error("Value passed to 'css' function must be a 'css' function result: "+t+". Use 'unsafeCSS' to pass non-literal values, but take care to ensure page security.")})(i)+t[r+1],t[0]);return new n(i,t,r)},o=i?t=>t:t=>t instanceof CSSStyleSheet?(t=>{let e="";for(const i of t.cssRules)e+=i.cssText;return(t=>new n("string"==typeof t?t:t+"",void 0,r))(e)})(t):t,{is:c,defineProperty:l,getOwnPropertyDescriptor:d,getOwnPropertyNames:h,getOwnPropertySymbols:p,getPrototypeOf:u}=Object,g=globalThis,m=g.trustedTypes,v=m?m.emptyScript:"",b=g.reactiveElementPolyfillSupport,f=(t,e)=>t,_={toAttribute(t,e){switch(e){case Boolean:t=t?v:null;break;case Object:case Array:t=null==t?t:JSON.stringify(t)}return t},fromAttribute(t,e){let i=t;switch(e){case Boolean:i=null!==t;break;case Number:i=null===t?null:Number(t);break;case Object:case Array:try{i=JSON.parse(t)}catch(t){i=null}}return i}},$=(t,e)=>!c(t,e),y={attribute:!0,type:String,converter:_,reflect:!1,useDefault:!1,hasChanged:$};Symbol.metadata??=Symbol("metadata"),g.litPropertyMetadata??=new WeakMap;let x=class extends HTMLElement{static addInitializer(t){this._$Ei(),(this.l??=[]).push(t)}static get observedAttributes(){return this.finalize(),this._$Eh&&[...this._$Eh.keys()]}static createProperty(t,e=y){if(e.state&&(e.attribute=!1),this._$Ei(),this.prototype.hasOwnProperty(t)&&((e=Object.create(e)).wrapped=!0),this.elementProperties.set(t,e),!e.noAccessor){const i=Symbol(),r=this.getPropertyDescriptor(t,i,e);void 0!==r&&l(this.prototype,t,r)}}static getPropertyDescriptor(t,e,i){const{get:r,set:s}=d(this.prototype,t)??{get(){return this[e]},set(t){this[e]=t}};return{get:r,set(e){const n=r?.call(this);s?.call(this,e),this.requestUpdate(t,n,i)},configurable:!0,enumerable:!0}}static getPropertyOptions(t){return this.elementProperties.get(t)??y}static _$Ei(){if(this.hasOwnProperty(f("elementProperties")))return;const t=u(this);t.finalize(),void 0!==t.l&&(this.l=[...t.l]),this.elementProperties=new Map(t.elementProperties)}static finalize(){if(this.hasOwnProperty(f("finalized")))return;if(this.finalized=!0,this._$Ei(),this.hasOwnProperty(f("properties"))){const t=this.properties,e=[...h(t),...p(t)];for(const i of e)this.createProperty(i,t[i])}const t=this[Symbol.metadata];if(null!==t){const e=litPropertyMetadata.get(t);if(void 0!==e)for(const[t,i]of e)this.elementProperties.set(t,i)}this._$Eh=new Map;for(const[t,e]of this.elementProperties){const i=this._$Eu(t,e);void 0!==i&&this._$Eh.set(i,t)}this.elementStyles=this.finalizeStyles(this.styles)}static finalizeStyles(t){const e=[];if(Array.isArray(t)){const i=new Set(t.flat(1/0).reverse());for(const t of i)e.unshift(o(t))}else void 0!==t&&e.push(o(t));return e}static _$Eu(t,e){const i=e.attribute;return!1===i?void 0:"string"==typeof i?i:"string"==typeof t?t.toLowerCase():void 0}constructor(){super(),this._$Ep=void 0,this.isUpdatePending=!1,this.hasUpdated=!1,this._$Em=null,this._$Ev()}_$Ev(){this._$ES=new Promise(t=>this.enableUpdating=t),this._$AL=new Map,this._$E_(),this.requestUpdate(),this.constructor.l?.forEach(t=>t(this))}addController(t){(this._$EO??=new Set).add(t),void 0!==this.renderRoot&&this.isConnected&&t.hostConnected?.()}removeController(t){this._$EO?.delete(t)}_$E_(){const t=new Map,e=this.constructor.elementProperties;for(const i of e.keys())this.hasOwnProperty(i)&&(t.set(i,this[i]),delete this[i]);t.size>0&&(this._$Ep=t)}createRenderRoot(){const t=this.shadowRoot??this.attachShadow(this.constructor.shadowRootOptions);return((t,r)=>{if(i)t.adoptedStyleSheets=r.map(t=>t instanceof CSSStyleSheet?t:t.styleSheet);else for(const i of r){const r=document.createElement("style"),s=e.litNonce;void 0!==s&&r.setAttribute("nonce",s),r.textContent=i.cssText,t.appendChild(r)}})(t,this.constructor.elementStyles),t}connectedCallback(){this.renderRoot??=this.createRenderRoot(),this.enableUpdating(!0),this._$EO?.forEach(t=>t.hostConnected?.())}enableUpdating(t){}disconnectedCallback(){this._$EO?.forEach(t=>t.hostDisconnected?.())}attributeChangedCallback(t,e,i){this._$AK(t,i)}_$ET(t,e){const i=this.constructor.elementProperties.get(t),r=this.constructor._$Eu(t,i);if(void 0!==r&&!0===i.reflect){const s=(void 0!==i.converter?.toAttribute?i.converter:_).toAttribute(e,i.type);this._$Em=t,null==s?this.removeAttribute(r):this.setAttribute(r,s),this._$Em=null}}_$AK(t,e){const i=this.constructor,r=i._$Eh.get(t);if(void 0!==r&&this._$Em!==r){const t=i.getPropertyOptions(r),s="function"==typeof t.converter?{fromAttribute:t.converter}:void 0!==t.converter?.fromAttribute?t.converter:_;this._$Em=r;const n=s.fromAttribute(e,t.type);this[r]=n??this._$Ej?.get(r)??n,this._$Em=null}}requestUpdate(t,e,i,r=!1,s){if(void 0!==t){const n=this.constructor;if(!1===r&&(s=this[t]),i??=n.getPropertyOptions(t),!((i.hasChanged??$)(s,e)||i.useDefault&&i.reflect&&s===this._$Ej?.get(t)&&!this.hasAttribute(n._$Eu(t,i))))return;this.C(t,e,i)}!1===this.isUpdatePending&&(this._$ES=this._$EP())}C(t,e,{useDefault:i,reflect:r,wrapped:s},n){i&&!(this._$Ej??=new Map).has(t)&&(this._$Ej.set(t,n??e??this[t]),!0!==s||void 0!==n)||(this._$AL.has(t)||(this.hasUpdated||i||(e=void 0),this._$AL.set(t,e)),!0===r&&this._$Em!==t&&(this._$Eq??=new Set).add(t))}async _$EP(){this.isUpdatePending=!0;try{await this._$ES}catch(t){Promise.reject(t)}const t=this.scheduleUpdate();return null!=t&&await t,!this.isUpdatePending}scheduleUpdate(){return this.performUpdate()}performUpdate(){if(!this.isUpdatePending)return;if(!this.hasUpdated){if(this.renderRoot??=this.createRenderRoot(),this._$Ep){for(const[t,e]of this._$Ep)this[t]=e;this._$Ep=void 0}const t=this.constructor.elementProperties;if(t.size>0)for(const[e,i]of t){const{wrapped:t}=i,r=this[e];!0!==t||this._$AL.has(e)||void 0===r||this.C(e,void 0,i,r)}}let t=!1;const e=this._$AL;try{t=this.shouldUpdate(e),t?(this.willUpdate(e),this._$EO?.forEach(t=>t.hostUpdate?.()),this.update(e)):this._$EM()}catch(e){throw t=!1,this._$EM(),e}t&&this._$AE(e)}willUpdate(t){}_$AE(t){this._$EO?.forEach(t=>t.hostUpdated?.()),this.hasUpdated||(this.hasUpdated=!0,this.firstUpdated(t)),this.updated(t)}_$EM(){this._$AL=new Map,this.isUpdatePending=!1}get updateComplete(){return this.getUpdateComplete()}getUpdateComplete(){return this._$ES}shouldUpdate(t){return!0}update(t){this._$Eq&&=this._$Eq.forEach(t=>this._$ET(t,this[t])),this._$EM()}updated(t){}firstUpdated(t){}};x.elementStyles=[],x.shadowRootOptions={mode:"open"},x[f("elementProperties")]=new Map,x[f("finalized")]=new Map,b?.({ReactiveElement:x}),(g.reactiveElementVersions??=[]).push("2.1.2");const w=globalThis,A=t=>t,S=w.trustedTypes,k=S?S.createPolicy("lit-html",{createHTML:t=>t}):void 0,E="$lit$",C=`lit$${Math.random().toFixed(9).slice(2)}$`,O="?"+C,P=`<${O}>`,M=document,N=()=>M.createComment(""),U=t=>null===t||"object"!=typeof t&&"function"!=typeof t,H=Array.isArray,z="[ \t\n\f\r]",T=/<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g,j=/-->/g,R=/>/g,I=RegExp(`>|${z}(?:([^\\s"'>=/]+)(${z}*=${z}*(?:[^ \t\n\f\r"'\`<>=]|("|')|))|$)`,"g"),D=/'/g,L=/"/g,W=/^(?:script|style|textarea|title)$/i,B=(t=>(e,...i)=>({_$litType$:t,strings:e,values:i}))(1),F=Symbol.for("lit-noChange"),q=Symbol.for("lit-nothing"),V=new WeakMap,J=M.createTreeWalker(M,129);function K(t,e){if(!H(t)||!t.hasOwnProperty("raw"))throw Error("invalid template strings array");return void 0!==k?k.createHTML(e):e}const Z=(t,e)=>{const i=t.length-1,r=[];let s,n=2===e?"<svg>":3===e?"<math>":"",a=T;for(let e=0;e<i;e++){const i=t[e];let o,c,l=-1,d=0;for(;d<i.length&&(a.lastIndex=d,c=a.exec(i),null!==c);)d=a.lastIndex,a===T?"!--"===c[1]?a=j:void 0!==c[1]?a=R:void 0!==c[2]?(W.test(c[2])&&(s=RegExp("</"+c[2],"g")),a=I):void 0!==c[3]&&(a=I):a===I?">"===c[0]?(a=s??T,l=-1):void 0===c[1]?l=-2:(l=a.lastIndex-c[2].length,o=c[1],a=void 0===c[3]?I:'"'===c[3]?L:D):a===L||a===D?a=I:a===j||a===R?a=T:(a=I,s=void 0);const h=a===I&&t[e+1].startsWith("/>")?" ":"";n+=a===T?i+P:l>=0?(r.push(o),i.slice(0,l)+E+i.slice(l)+C+h):i+C+(-2===l?e:h)}return[K(t,n+(t[i]||"<?>")+(2===e?"</svg>":3===e?"</math>":"")),r]};class Q{constructor({strings:t,_$litType$:e},i){let r;this.parts=[];let s=0,n=0;const a=t.length-1,o=this.parts,[c,l]=Z(t,e);if(this.el=Q.createElement(c,i),J.currentNode=this.el.content,2===e||3===e){const t=this.el.content.firstChild;t.replaceWith(...t.childNodes)}for(;null!==(r=J.nextNode())&&o.length<a;){if(1===r.nodeType){if(r.hasAttributes())for(const t of r.getAttributeNames())if(t.endsWith(E)){const e=l[n++],i=r.getAttribute(t).split(C),a=/([.?@])?(.*)/.exec(e);o.push({type:1,index:s,name:a[2],strings:i,ctor:"."===a[1]?et:"?"===a[1]?it:"@"===a[1]?rt:tt}),r.removeAttribute(t)}else t.startsWith(C)&&(o.push({type:6,index:s}),r.removeAttribute(t));if(W.test(r.tagName)){const t=r.textContent.split(C),e=t.length-1;if(e>0){r.textContent=S?S.emptyScript:"";for(let i=0;i<e;i++)r.append(t[i],N()),J.nextNode(),o.push({type:2,index:++s});r.append(t[e],N())}}}else if(8===r.nodeType)if(r.data===O)o.push({type:2,index:s});else{let t=-1;for(;-1!==(t=r.data.indexOf(C,t+1));)o.push({type:7,index:s}),t+=C.length-1}s++}}static createElement(t,e){const i=M.createElement("template");return i.innerHTML=t,i}}function G(t,e,i=t,r){if(e===F)return e;let s=void 0!==r?i._$Co?.[r]:i._$Cl;const n=U(e)?void 0:e._$litDirective$;return s?.constructor!==n&&(s?._$AO?.(!1),void 0===n?s=void 0:(s=new n(t),s._$AT(t,i,r)),void 0!==r?(i._$Co??=[])[r]=s:i._$Cl=s),void 0!==s&&(e=G(t,s._$AS(t,e.values),s,r)),e}class X{constructor(t,e){this._$AV=[],this._$AN=void 0,this._$AD=t,this._$AM=e}get parentNode(){return this._$AM.parentNode}get _$AU(){return this._$AM._$AU}u(t){const{el:{content:e},parts:i}=this._$AD,r=(t?.creationScope??M).importNode(e,!0);J.currentNode=r;let s=J.nextNode(),n=0,a=0,o=i[0];for(;void 0!==o;){if(n===o.index){let e;2===o.type?e=new Y(s,s.nextSibling,this,t):1===o.type?e=new o.ctor(s,o.name,o.strings,this,t):6===o.type&&(e=new st(s,this,t)),this._$AV.push(e),o=i[++a]}n!==o?.index&&(s=J.nextNode(),n++)}return J.currentNode=M,r}p(t){let e=0;for(const i of this._$AV)void 0!==i&&(void 0!==i.strings?(i._$AI(t,i,e),e+=i.strings.length-2):i._$AI(t[e])),e++}}class Y{get _$AU(){return this._$AM?._$AU??this._$Cv}constructor(t,e,i,r){this.type=2,this._$AH=q,this._$AN=void 0,this._$AA=t,this._$AB=e,this._$AM=i,this.options=r,this._$Cv=r?.isConnected??!0}get parentNode(){let t=this._$AA.parentNode;const e=this._$AM;return void 0!==e&&11===t?.nodeType&&(t=e.parentNode),t}get startNode(){return this._$AA}get endNode(){return this._$AB}_$AI(t,e=this){t=G(this,t,e),U(t)?t===q||null==t||""===t?(this._$AH!==q&&this._$AR(),this._$AH=q):t!==this._$AH&&t!==F&&this._(t):void 0!==t._$litType$?this.$(t):void 0!==t.nodeType?this.T(t):(t=>H(t)||"function"==typeof t?.[Symbol.iterator])(t)?this.k(t):this._(t)}O(t){return this._$AA.parentNode.insertBefore(t,this._$AB)}T(t){this._$AH!==t&&(this._$AR(),this._$AH=this.O(t))}_(t){this._$AH!==q&&U(this._$AH)?this._$AA.nextSibling.data=t:this.T(M.createTextNode(t)),this._$AH=t}$(t){const{values:e,_$litType$:i}=t,r="number"==typeof i?this._$AC(t):(void 0===i.el&&(i.el=Q.createElement(K(i.h,i.h[0]),this.options)),i);if(this._$AH?._$AD===r)this._$AH.p(e);else{const t=new X(r,this),i=t.u(this.options);t.p(e),this.T(i),this._$AH=t}}_$AC(t){let e=V.get(t.strings);return void 0===e&&V.set(t.strings,e=new Q(t)),e}k(t){H(this._$AH)||(this._$AH=[],this._$AR());const e=this._$AH;let i,r=0;for(const s of t)r===e.length?e.push(i=new Y(this.O(N()),this.O(N()),this,this.options)):i=e[r],i._$AI(s),r++;r<e.length&&(this._$AR(i&&i._$AB.nextSibling,r),e.length=r)}_$AR(t=this._$AA.nextSibling,e){for(this._$AP?.(!1,!0,e);t!==this._$AB;){const e=A(t).nextSibling;A(t).remove(),t=e}}setConnected(t){void 0===this._$AM&&(this._$Cv=t,this._$AP?.(t))}}class tt{get tagName(){return this.element.tagName}get _$AU(){return this._$AM._$AU}constructor(t,e,i,r,s){this.type=1,this._$AH=q,this._$AN=void 0,this.element=t,this.name=e,this._$AM=r,this.options=s,i.length>2||""!==i[0]||""!==i[1]?(this._$AH=Array(i.length-1).fill(new String),this.strings=i):this._$AH=q}_$AI(t,e=this,i,r){const s=this.strings;let n=!1;if(void 0===s)t=G(this,t,e,0),n=!U(t)||t!==this._$AH&&t!==F,n&&(this._$AH=t);else{const r=t;let a,o;for(t=s[0],a=0;a<s.length-1;a++)o=G(this,r[i+a],e,a),o===F&&(o=this._$AH[a]),n||=!U(o)||o!==this._$AH[a],o===q?t=q:t!==q&&(t+=(o??"")+s[a+1]),this._$AH[a]=o}n&&!r&&this.j(t)}j(t){t===q?this.element.removeAttribute(this.name):this.element.setAttribute(this.name,t??"")}}class et extends tt{constructor(){super(...arguments),this.type=3}j(t){this.element[this.name]=t===q?void 0:t}}class it extends tt{constructor(){super(...arguments),this.type=4}j(t){this.element.toggleAttribute(this.name,!!t&&t!==q)}}class rt extends tt{constructor(t,e,i,r,s){super(t,e,i,r,s),this.type=5}_$AI(t,e=this){if((t=G(this,t,e,0)??q)===F)return;const i=this._$AH,r=t===q&&i!==q||t.capture!==i.capture||t.once!==i.once||t.passive!==i.passive,s=t!==q&&(i===q||r);r&&this.element.removeEventListener(this.name,this,i),s&&this.element.addEventListener(this.name,this,t),this._$AH=t}handleEvent(t){"function"==typeof this._$AH?this._$AH.call(this.options?.host??this.element,t):this._$AH.handleEvent(t)}}class st{constructor(t,e,i){this.element=t,this.type=6,this._$AN=void 0,this._$AM=e,this.options=i}get _$AU(){return this._$AM._$AU}_$AI(t){G(this,t)}}const nt=w.litHtmlPolyfillSupport;nt?.(Q,Y),(w.litHtmlVersions??=[]).push("3.3.3");const at=globalThis;class ot extends x{constructor(){super(...arguments),this.renderOptions={host:this},this._$Do=void 0}createRenderRoot(){const t=super.createRenderRoot();return this.renderOptions.renderBefore??=t.firstChild,t}update(t){const e=this.render();this.hasUpdated||(this.renderOptions.isConnected=this.isConnected),super.update(t),this._$Do=((t,e,i)=>{const r=i?.renderBefore??e;let s=r._$litPart$;if(void 0===s){const t=i?.renderBefore??null;r._$litPart$=s=new Y(e.insertBefore(N(),t),t,void 0,i??{})}return s._$AI(t),s})(e,this.renderRoot,this.renderOptions)}connectedCallback(){super.connectedCallback(),this._$Do?.setConnected(!0)}disconnectedCallback(){super.disconnectedCallback(),this._$Do?.setConnected(!1)}render(){return F}}ot._$litElement$=!0,ot.finalized=!0,at.litElementHydrateSupport?.({LitElement:ot});const ct=at.litElementPolyfillSupport;ct?.({LitElement:ot}),(at.litElementVersions??=[]).push("4.2.2");const lt=t=>(e,i)=>{void 0!==i?i.addInitializer(()=>{customElements.define(t,e)}):customElements.define(t,e)},dt={attribute:!0,type:String,converter:_,reflect:!1,hasChanged:$},ht=(t=dt,e,i)=>{const{kind:r,metadata:s}=i;let n=globalThis.litPropertyMetadata.get(s);if(void 0===n&&globalThis.litPropertyMetadata.set(s,n=new Map),"setter"===r&&((t=Object.create(t)).wrapped=!0),n.set(i.name,t),"accessor"===r){const{name:r}=i;return{set(i){const s=e.get.call(this);e.set.call(this,i),this.requestUpdate(r,s,t,!0,i)},init(e){return void 0!==e&&this.C(r,void 0,t,e),e}}}if("setter"===r){const{name:r}=i;return function(i){const s=this[r];e.call(this,i),this.requestUpdate(r,s,t,!0,i)}}throw Error("Unsupported decorator location: "+r)};function pt(t){return(e,i)=>"object"==typeof i?ht(t,e,i):((t,e,i)=>{const r=e.hasOwnProperty(i);return e.constructor.createProperty(i,t),r?Object.getOwnPropertyDescriptor(e,i):void 0})(t,e,i)}function ut(t){return pt({...t,state:!0,attribute:!1})}function gt(t,e,i){for(const r of e){const[e]=r.split(".",1),s=r.split(".")[1]??"",n=i.states[r]?.attributes.device_class;"sensor"===e?"temperature"===n?t.temperature=r:"humidity"===n?t.humidity=r:"illuminance"===n&&(t.light=r):"binary_sensor"===e?"motion"===n||s.endsWith("_motion")||s.endsWith("_cloud_motion")?t.motion=r:("sound"===n||s.endsWith("_sound")||s.endsWith("_cloud_sound"))&&(t.sound=r):"switch"===e&&s.endsWith("_camera_power")?t.power=r:"light"===e&&s.endsWith("_night_light")&&!s.includes("sl_")?t.night_light=r:"media_player"===e&&s.endsWith("_sound_machine")&&(t.sound_machine=r)}}function mt(t,e){if(!e)return!1;const i=t.entities[e];return!i?.disabled_by&&e in t.states}const vt=a`
  :host {
    --nanit-radius: 14px;
    --nanit-pill-bg: rgba(0, 0, 0, 0.5);
    --nanit-pill-radius: 16px;
    --nanit-transition: 0.3s ease;
    --nanit-gap: 10px;
    --nanit-amber: rgb(201, 168, 76);
    --nanit-amber-glow: rgba(201, 168, 76, 0.3);
    --nanit-teal: rgb(50, 160, 200);
    --nanit-teal-glow: rgba(50, 160, 200, 0.3);
  }

  ha-card {
    overflow: hidden;
    border-radius: var(--ha-card-border-radius, var(--nanit-radius));
    background: var(--ha-card-background, var(--card-background-color));
    color: var(--primary-text-color);
    border: 1px solid rgba(201, 168, 76, 0.25);
  }

  /* -- Header -- */

  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
  }

  .device-badge {
    display: flex;
    align-items: center;
    gap: 8px;
    background: rgba(201, 168, 76, 0.2);
    padding: 6px 14px 6px 8px;
    border-radius: 24px;
    box-shadow: 0 0 10px var(--nanit-amber-glow);
    transition: background var(--nanit-transition),
                box-shadow var(--nanit-transition);
  }

  .device-badge:hover {
    background: rgba(201, 168, 76, 0.3);
    box-shadow: 0 0 16px var(--nanit-amber-glow);
  }

  .device-badge ha-icon {
    --mdc-icon-size: 22px;
    color: var(--nanit-amber);
  }

  .device-name {
    font-size: 15px;
    font-weight: 500;
    color: var(--primary-text-color);
    letter-spacing: 0.01em;
  }

  .power-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(201, 168, 76, 0.2);
    border: none;
    padding: 8px;
    border-radius: 50%;
    cursor: pointer;
    color: var(--nanit-amber);
    transition: background var(--nanit-transition),
                color var(--nanit-transition),
                box-shadow var(--nanit-transition);
    box-shadow: 0 0 10px var(--nanit-amber-glow);
  }

  .power-btn:hover {
    background: rgba(201, 168, 76, 0.3);
    box-shadow: 0 0 16px var(--nanit-amber-glow);
  }

  .power-btn.off {
    background: rgba(127, 127, 127, 0.1);
    color: var(--disabled-text-color);
    box-shadow: none;
  }

  .power-btn.off:hover {
    background: rgba(127, 127, 127, 0.18);
  }

  .power-btn ha-icon {
    --mdc-icon-size: 24px;
  }

  /* -- Camera Off -- */

  .camera-off-label {
    font-size: 13px;
    color: var(--secondary-text-color);
    margin-right: auto;
    padding-left: 4px;
  }

  /* -- Stream Container -- */

  .stream-wrap {
    position: relative;
    overflow: hidden;
    background: #000;
    border-radius: var(--nanit-radius);
    margin: 0 8px;
  }

  .stream-click {
    cursor: pointer;
  }

  .stream-click ha-camera-stream {
    display: block;
    width: 100%;
  }

  .stream-placeholder {
    aspect-ratio: 16 / 9;
    display: flex;
    align-items: center;
    justify-content: center;
    color: rgba(255, 255, 255, 0.4);
    cursor: pointer;
  }

  .stream-placeholder ha-icon {
    --mdc-icon-size: 48px;
  }

  /* -- Sensor Overlays -- */

  .overlay-top {
    position: absolute;
    top: 8px;
    left: 8px;
    right: 8px;
    display: flex;
    justify-content: space-between;
    z-index: 2;
    pointer-events: none;
  }

  .overlay-top .pill {
    pointer-events: auto;
  }

  .pill {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 4px 10px;
    background: var(--nanit-pill-bg);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    border-radius: var(--nanit-pill-radius);
    font-size: 12px;
    font-weight: 500;
    color: #fff;
    cursor: pointer;
    transition: transform var(--nanit-transition), box-shadow var(--nanit-transition);
    line-height: 1;
    user-select: none;
  }

  .pill:hover {
    transform: scale(1.05);
  }

  .pill ha-icon {
    --mdc-icon-size: 14px;
    color: rgba(255, 255, 255, 0.85);
  }

  .pill-temp {
    color: var(--nanit-amber);
  }

  .pill-temp ha-icon {
    color: var(--nanit-amber);
  }

  .pill-humid {
    color: var(--nanit-teal);
  }

  .pill-humid ha-icon {
    color: var(--nanit-teal);
  }

  .pill-light {
    color: var(--nanit-amber);
  }

  .pill-light ha-icon {
    color: var(--nanit-amber);
  }

  /* -- Motion / Sound Overlays -- */

  .overlay-bottom {
    position: absolute;
    bottom: 8px;
    left: 8px;
    right: 8px;
    display: flex;
    justify-content: space-between;
    z-index: 2;
    pointer-events: none;
  }

  .overlay-bottom .pill {
    pointer-events: auto;
  }

  .pill.active {
    animation: pulse 1.6s ease-in-out infinite;
  }

  .pill.motion-active {
    background: rgba(201, 168, 76, 0.75);
    box-shadow: 0 0 16px rgba(201, 168, 76, 0.5), 0 0 32px rgba(201, 168, 76, 0.2);
  }

  .pill.sound-active {
    background: rgba(50, 160, 200, 0.75);
    box-shadow: 0 0 16px rgba(50, 160, 200, 0.5), 0 0 32px rgba(50, 160, 200, 0.2);
  }

  @keyframes pulse {
    0%, 100% {
      transform: scale(1);
      opacity: 1;
    }
    50% {
      transform: scale(1.08);
      opacity: 0.85;
    }
  }

  /* -- Controls Container -- */

  .controls {
    display: flex;
    flex-direction: column;
    gap: var(--nanit-gap);
    padding: var(--nanit-gap) 8px 8px;
  }

  /* -- Control Sections (Night Light + Sound Machine) -- */

  .control-section {
    display: flex;
    flex-direction: column;
    gap: 8px;
    border-radius: var(--nanit-radius);
    padding: 14px;
    transition: background var(--nanit-transition);
  }

  .control-section-light {
    background: rgba(201, 168, 76, 0.1);
    border: 1px solid rgba(201, 168, 76, 0.2);
  }

  .control-section-sound {
    background: rgba(50, 160, 200, 0.1);
    border: 1px solid rgba(50, 160, 200, 0.2);
  }

  .control-row {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .control-label {
    font-size: 13px;
    font-weight: 500;
    color: var(--secondary-text-color);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .section-header .source-list {
    flex: 1;
    min-width: 0;
    justify-content: flex-end;
  }

  .icon-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: 2px solid var(--divider-color, rgba(127, 127, 127, 0.2));
    width: 36px;
    height: 36px;
    border-radius: 50%;
    cursor: pointer;
    color: var(--primary-text-color);
    transition: background var(--nanit-transition),
                border-color var(--nanit-transition),
                color var(--nanit-transition),
                box-shadow var(--nanit-transition);
    flex-shrink: 0;
    padding: 0;
  }

  .icon-btn:hover {
    background: var(--secondary-background-color, rgba(127, 127, 127, 0.12));
  }

  .icon-btn.active {
    border-color: var(--primary-color);
    color: var(--primary-color);
    background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.1);
    box-shadow: 0 0 8px rgba(var(--rgb-primary-color, 3, 169, 244), 0.2);
  }

  .control-section-light .icon-btn.active {
    border-color: var(--nanit-amber);
    color: var(--nanit-amber);
    background: rgba(201, 168, 76, 0.15);
    box-shadow: 0 0 8px var(--nanit-amber-glow);
  }

  .control-section-sound .icon-btn.active {
    border-color: var(--nanit-teal);
    color: var(--nanit-teal);
    background: rgba(50, 160, 200, 0.15);
    box-shadow: 0 0 8px var(--nanit-teal-glow);
  }

  .icon-btn ha-icon {
    --mdc-icon-size: 18px;
  }

  .slider-row {
    display: flex;
    align-items: center;
    gap: 10px;
    flex: 1;
    min-width: 0;
  }

  /* -- Custom Fat Slider -- */

  .nanit-slider {
    position: relative;
    flex: 1;
    min-width: 0;
    height: 28px;
    display: flex;
    align-items: center;
  }

  .nanit-slider input[type="range"] {
    -webkit-appearance: none;
    appearance: none;
    width: 100%;
    height: 14px;
    border-radius: 7px;
    outline: none;
    cursor: pointer;
    margin: 0;
    background: linear-gradient(
      to right,
      var(--nanit-slider-active, var(--nanit-amber)) 0%,
      var(--nanit-slider-active, var(--nanit-amber)) var(--slider-pct, 0%),
      var(--nanit-slider-track, rgba(201, 168, 76, 0.15)) var(--slider-pct, 0%),
      var(--nanit-slider-track, rgba(201, 168, 76, 0.15)) 100%
    );
    transition: box-shadow 0.2s ease;
  }

  .nanit-slider input[type="range"]:hover {
    box-shadow: 0 0 8px var(--nanit-slider-glow, var(--nanit-amber-glow));
  }

  /* Webkit thumb */
  .nanit-slider input[type="range"]::-webkit-slider-thumb {
    -webkit-appearance: none;
    appearance: none;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background: var(--nanit-slider-thumb, var(--nanit-amber));
    border: 2px solid var(--ha-card-background, var(--card-background-color, #fff));
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.3);
    cursor: pointer;
    transition: transform 0.15s ease, box-shadow 0.15s ease;
  }

  .nanit-slider input[type="range"]::-webkit-slider-thumb:hover {
    transform: scale(1.15);
    box-shadow: 0 0 10px var(--nanit-slider-glow, var(--nanit-amber-glow)),
                0 2px 6px rgba(0, 0, 0, 0.3);
  }

  .nanit-slider input[type="range"]:active::-webkit-slider-thumb {
    transform: scale(1.05);
  }

  /* Firefox thumb */
  .nanit-slider input[type="range"]::-moz-range-thumb {
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background: var(--nanit-slider-thumb, var(--nanit-amber));
    border: 2px solid var(--ha-card-background, var(--card-background-color, #fff));
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.3);
    cursor: pointer;
    transition: transform 0.15s ease, box-shadow 0.15s ease;
  }

  .nanit-slider input[type="range"]::-moz-range-thumb:hover {
    transform: scale(1.15);
    box-shadow: 0 0 10px var(--nanit-slider-glow, var(--nanit-amber-glow)),
                0 2px 6px rgba(0, 0, 0, 0.3);
  }

  /* Firefox track (needed for FF) */
  .nanit-slider input[type="range"]::-moz-range-track {
    height: 14px;
    border-radius: 7px;
    background: transparent;
    border: none;
  }

  /* Sound machine slider color overrides */
  .control-section-sound .nanit-slider input[type="range"] {
    background: linear-gradient(
      to right,
      var(--nanit-teal) 0%,
      var(--nanit-teal) var(--slider-pct, 0%),
      rgba(50, 160, 200, 0.15) var(--slider-pct, 0%),
      rgba(50, 160, 200, 0.15) 100%
    );
  }

  .control-section-sound .nanit-slider input[type="range"]:hover {
    box-shadow: 0 0 8px var(--nanit-teal-glow);
  }

  .control-section-sound .nanit-slider input[type="range"]::-webkit-slider-thumb {
    background: var(--nanit-teal);
  }

  .control-section-sound .nanit-slider input[type="range"]::-webkit-slider-thumb:hover {
    box-shadow: 0 0 10px var(--nanit-teal-glow),
                0 2px 6px rgba(0, 0, 0, 0.3);
  }

  .control-section-sound .nanit-slider input[type="range"]::-moz-range-thumb {
    background: var(--nanit-teal);
  }

  .control-section-sound .nanit-slider input[type="range"]::-moz-range-thumb:hover {
    box-shadow: 0 0 10px var(--nanit-teal-glow),
                0 2px 6px rgba(0, 0, 0, 0.3);
  }

  .slider-value {
    font-size: 12px;
    font-weight: 500;
    color: var(--secondary-text-color);
    min-width: 32px;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  /* -- Sound Machine -- */

  .track-name {
    font-size: 13px;
    color: var(--primary-text-color);
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .source-list {
    display: flex;
    gap: 4px;
    overflow-x: auto;
    padding: 2px 0;
    scrollbar-width: none;
    -ms-overflow-style: none;
  }

  .source-list::-webkit-scrollbar {
    display: none;
  }

  .source-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border-radius: 50%;
    border: 1.5px solid var(--divider-color, rgba(127, 127, 127, 0.2));
    background: none;
    color: var(--primary-text-color);
    cursor: pointer;
    padding: 0;
    transition: background var(--nanit-transition),
                border-color var(--nanit-transition),
                color var(--nanit-transition),
                box-shadow var(--nanit-transition);
  }

  .source-icon ha-icon {
    --mdc-icon-size: 15px;
  }

  .source-icon:hover {
    background: var(--secondary-background-color, rgba(127, 127, 127, 0.1));
  }

  .source-icon.active {
    background: rgba(50, 160, 200, 0.15);
    border-color: var(--nanit-teal);
    color: var(--nanit-teal);
    box-shadow: 0 0 6px var(--nanit-teal-glow);
  }

  /* -- Collapse transition -- */

  .card-content {
    overflow: hidden;
    transition: max-height 0.4s ease, opacity 0.3s ease;
    max-height: 800px;
    opacity: 1;
  }

  .card-content.collapsed {
    max-height: 0;
    opacity: 0;
  }
`;let bt=class extends ot{setConfig(t){this._config={...t}}_entityChanged(t){const e=t.detail.value;if(!this._config||e===this._config.camera_entity_id)return;const i={...this._config,camera_entity_id:e};this._config=i,this.dispatchEvent(new CustomEvent("config-changed",{bubbles:!0,composed:!0,detail:{config:i}}))}render(){return this.hass&&this._config?B`
      <div class="editor">
        <ha-entity-picker
          .hass=${this.hass}
          .value=${this._config.camera_entity_id||""}
          .includeDomains=${["camera"]}
          .label=${"Camera Entity"}
          allow-custom-entity
          @value-changed=${this._entityChanged}
        ></ha-entity-picker>
      </div>
    `:q}};bt.styles=a`
    .editor {
      padding: 16px;
    }
    ha-entity-picker {
      display: block;
    }
  `,t([pt({attribute:!1})],bt.prototype,"hass",void 0),t([ut()],bt.prototype,"_config",void 0),bt=t([lt("nanit-card-editor")],bt);let ft=class extends ot{static getConfigElement(){return document.createElement("nanit-card-editor")}static getStubConfig(t){const e=Object.keys(t.states).find(e=>e.startsWith("camera.")&&"nanit"===t.entities[e]?.platform);return{type:"custom:nanit-card",camera_entity_id:e||""}}setConfig(t){if(!t)throw new Error("Invalid configuration");this._config=t}getCardSize(){return 5}_entities(){return this._config?.camera_entity_id&&this.hass?function(t,e){const i={};i.camera=e;const r=t.entities[e];if(r?.device_id){const e=r.device_id,s=[];for(const i of Object.keys(t.entities))t.entities[i].device_id===e&&"diagnostic"!==t.entities[i].entity_category&&s.push(i);gt(i,s,t)}else{const r=e.split(".")[1]??"";gt(i,Object.keys(t.states).filter(t=>t!==e&&t.split(".")[1]?.startsWith(r.split("_camera")[0]||r)),t)}return i}(this.hass,this._config.camera_entity_id):{}}_isCameraOn(t){return!t.power||"on"===this.hass.states[t.power]?.state}_fireMoreInfo(t){this.dispatchEvent(new CustomEvent("hass-more-info",{bubbles:!0,composed:!0,detail:{entityId:t}}))}_toggleService(t,e,i){this.hass.callService(t,e,{entity_id:i})}render(){if(!this.hass||!this._config)return B`<ha-card><div class="header"><span class="device-name">Nanit</span></div></ha-card>`;const t=this._entities(),e=this._isCameraOn(t),i=t.camera?function(t,e){const i=t.states[e];return i?(i.attributes.friendly_name??"Nanit").replace(/ Camera$/i,""):"Nanit"}(this.hass,t.camera):"Nanit";return B`
      <ha-card>
        ${this._renderHeader(i,t,e)}
        <div class="card-content ${e?"":"collapsed"}">
          ${e?this._renderStream(t):q}
          ${e?this._renderControls(t):q}
        </div>
      </ha-card>
    `}_renderHeader(t,e,i){return B`
      <div class="header">
        <div class="device-badge">
          <ha-icon icon="mdi:baby-face-outline"></ha-icon>
          <span class="device-name">${t}</span>
        </div>
        ${i?q:B`<span class="camera-off-label">Camera Off</span>`}
        ${e.power?B`
              <button
                class="power-btn ${i?"":"off"}"
                @click=${()=>this._toggleService("switch","toggle",e.power)}
              >
                <ha-icon icon="mdi:power"></ha-icon>
              </button>
            `:q}
      </div>
    `}_renderStream(t){const e=t.camera?this.hass.states[t.camera]:void 0;return B`
      <div class="stream-wrap">
        ${e?B`
              <div
                class="stream-click"
                @click=${()=>t.camera&&this._fireMoreInfo(t.camera)}
              >
                <ha-camera-stream
                  muted
                  .hass=${this.hass}
                  .stateObj=${e}
                ></ha-camera-stream>
              </div>
            `:B`
              <div
                class="stream-placeholder"
                @click=${()=>t.camera&&this._fireMoreInfo(t.camera)}
              >
                <ha-icon icon="mdi:camera-off"></ha-icon>
              </div>
            `}
        ${this._renderSensorOverlays(t)}
        ${this._renderDetectionOverlays(t)}
      </div>
    `}_renderSensorOverlays(t){const e=[];if(mt(this.hass,t.temperature)){const i=parseFloat(this.hass.states[t.temperature].state),r=isNaN(i)?this.hass.states[t.temperature].state:i.toFixed(1),s=this.hass.states[t.temperature].attributes.unit_of_measurement??"";e.push(B`
        <div class="pill pill-temp" @click=${()=>this._fireMoreInfo(t.temperature)}>
          <ha-icon icon="mdi:thermometer"></ha-icon>
          <span>${r}${s}</span>
        </div>
      `)}if(mt(this.hass,t.humidity)){const i=parseFloat(this.hass.states[t.humidity].state),r=isNaN(i)?this.hass.states[t.humidity].state:i.toFixed(1);e.push(B`
        <div class="pill pill-humid" @click=${()=>this._fireMoreInfo(t.humidity)}>
          <ha-icon icon="mdi:water-percent"></ha-icon>
          <span>${r}%</span>
        </div>
      `)}if(mt(this.hass,t.light)){const i=parseFloat(this.hass.states[t.light].state),r=isNaN(i)?this.hass.states[t.light].state:Math.round(i).toString();e.push(B`
        <div class="pill pill-light" @click=${()=>this._fireMoreInfo(t.light)}>
          <ha-icon icon="mdi:brightness-5"></ha-icon>
          <span>${r} lx</span>
        </div>
      `)}return 0===e.length?B``:B`<div class="overlay-top">${e}</div>`}_renderDetectionOverlays(t){const e=mt(this.hass,t.motion),i=mt(this.hass,t.sound);if(!e&&!i)return B``;const r=e&&"on"===this.hass.states[t.motion].state,s=i&&"on"===this.hass.states[t.sound].state;return B`
      <div class="overlay-bottom">
        ${e?B`
              <div
                class="pill ${r?"active motion-active":""}"
                @click=${()=>this._fireMoreInfo(t.motion)}
              >
                <ha-icon icon="mdi:motion-sensor"></ha-icon>
                <span>${r?"Motion":"Clear"}</span>
              </div>
            `:B`<div></div>`}
        ${i?B`
              <div
                class="pill ${s?"active sound-active":""}"
                @click=${()=>this._fireMoreInfo(t.sound)}
              >
                <ha-icon icon="mdi:ear-hearing"></ha-icon>
                <span>${s?"Sound":"Quiet"}</span>
              </div>
            `:q}
      </div>
    `}_renderControls(t){const e=mt(this.hass,t.night_light),i=mt(this.hass,t.sound_machine);return e||i?B`
      <div class="controls">
        ${e?this._renderNightLight(t.night_light):q}
        ${i?this._renderSoundMachine(t.sound_machine):q}
      </div>
    `:B``}_renderNightLight(t){const e=this.hass.states[t],i="on"===e?.state,r=e?.attributes.brightness??0,s=Math.round(r/255*100);return B`
      <div class="control-section control-section-light">
        <span class="control-label">Night Light</span>
        <div class="control-row">
          <button
            class="icon-btn ${i?"active":""}"
            @click=${()=>this._toggleService("light","toggle",t)}
          >
            <ha-icon icon="mdi:lightbulb${i?"":"-outline"}"></ha-icon>
          </button>
          <div class="slider-row">
            <div class="nanit-slider" style="--slider-pct: ${s}%">
              <input
                type="range"
                min="0"
                max="100"
                .value=${String(s)}
                @input=${t=>{const e=t.target.closest(".nanit-slider");e&&e.style.setProperty("--slider-pct",`${t.target.value}%`)}}
                @change=${e=>{const i=Number(e.target.value);0===i?this.hass.callService("light","turn_off",{entity_id:t}):this.hass.callService("light","turn_on",{entity_id:t,brightness:Math.round(i/100*255)})}}
              />
            </div>
            <span class="slider-value">${i?`${s}%`:"Off"}</span>
          </div>
        </div>
      </div>
    `}_renderSoundMachine(t){const e=this.hass.states[t],i="playing"===e?.state,r=e?.attributes.source??"",s=e?.attributes.source_list??[],n=e?.attributes.volume_level??0,a=Math.round(100*n);return B`
      <div class="control-section control-section-sound">
        <div class="section-header">
          <span class="control-label">Sound Machine</span>
          ${s.length>0?B`
                <div class="source-list">
                  ${s.map(e=>B`
                      <button
                        class="source-icon ${e===r?"active":""}"
                        title=${this._formatSourceName(e)}
                        @click=${()=>this.hass.callService("media_player","select_source",{entity_id:t,source:e})}
                      >
                        <ha-icon icon=${this._sourceIcon(e)}></ha-icon>
                      </button>
                    `)}
                </div>
              `:q}
        </div>
        <div class="control-row">
          <button
            class="icon-btn ${i?"active":""}"
            @click=${()=>this._toggleService("media_player",i?"media_stop":"media_play",t)}
          >
            <ha-icon icon="mdi:${i?"stop":"play"}"></ha-icon>
          </button>
          ${i?B`<span class="track-name">${this._formatSourceName(r)}</span>`:q}
          <div class="slider-row">
            <div class="nanit-slider" style="--slider-pct: ${a}%">
              <input
                type="range"
                min="0"
                max="100"
                .value=${String(a)}
                @input=${t=>{const e=t.target.closest(".nanit-slider");e&&e.style.setProperty("--slider-pct",`${t.target.value}%`)}}
                @change=${e=>{const i=Number(e.target.value);this.hass.callService("media_player","volume_set",{entity_id:t,volume_level:i/100})}}
              />
            </div>
            <span class="slider-value">${a}%</span>
          </div>
        </div>
      </div>
    `}_sourceIcon(t){return{white_noise:"mdi:sine-wave",birds:"mdi:bird",waves:"mdi:waves",wind:"mdi:weather-windy",rain:"mdi:weather-rainy",water_stream:"mdi:water",fan:"mdi:fan",heartbeat:"mdi:heart-pulse",dryer:"mdi:tumble-dryer",vacuum:"mdi:robot-vacuum"}[t.replace(/\.wav$/i,"").toLowerCase()]??"mdi:music-note"}_formatSourceName(t){return t.replace(/\.wav$/i,"").replace(/_/g," ")}};ft.styles=vt,t([pt({attribute:!1})],ft.prototype,"hass",void 0),t([ut()],ft.prototype,"_config",void 0),ft=t([lt("nanit-card")],ft),window.customCards=window.customCards||[],window.customCards.push({type:"nanit-card",name:"Nanit Camera",description:"Camera stream with controls for Nanit baby cameras",preview:!0});export{ft as NanitCard};
