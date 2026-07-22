function t(t,e,i,s){var r,a=arguments.length,n=a<3?e:null===s?s=Object.getOwnPropertyDescriptor(e,i):s;if("object"==typeof Reflect&&"function"==typeof Reflect.decorate)n=Reflect.decorate(t,e,i,s);else for(var o=t.length-1;o>=0;o--)(r=t[o])&&(n=(a<3?r(n):a>3?r(e,i,n):r(e,i))||n);return a>3&&n&&Object.defineProperty(e,i,n),n}"function"==typeof SuppressedError&&SuppressedError;const e=globalThis,i=e.ShadowRoot&&(void 0===e.ShadyCSS||e.ShadyCSS.nativeShadow)&&"adoptedStyleSheets"in Document.prototype&&"replace"in CSSStyleSheet.prototype,s=Symbol(),r=new WeakMap;let a=class{constructor(t,e,i){if(this._$cssResult$=!0,i!==s)throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");this.cssText=t,this.t=e}get styleSheet(){let t=this.o;const e=this.t;if(i&&void 0===t){const i=void 0!==e&&1===e.length;i&&(t=r.get(e)),void 0===t&&((this.o=t=new CSSStyleSheet).replaceSync(this.cssText),i&&r.set(e,t))}return t}toString(){return this.cssText}};const n=(t,...e)=>{const i=1===t.length?t[0]:e.reduce((e,i,s)=>e+(t=>{if(!0===t._$cssResult$)return t.cssText;if("number"==typeof t)return t;throw Error("Value passed to 'css' function must be a 'css' function result: "+t+". Use 'unsafeCSS' to pass non-literal values, but take care to ensure page security.")})(i)+t[s+1],t[0]);return new a(i,t,s)},o=i?t=>t:t=>t instanceof CSSStyleSheet?(t=>{let e="";for(const i of t.cssRules)e+=i.cssText;return(t=>new a("string"==typeof t?t:t+"",void 0,s))(e)})(t):t,{is:c,defineProperty:l,getOwnPropertyDescriptor:h,getOwnPropertyNames:d,getOwnPropertySymbols:p,getPrototypeOf:u}=Object,m=globalThis,_=m.trustedTypes,g=_?_.emptyScript:"",v=m.reactiveElementPolyfillSupport,b=(t,e)=>t,f={toAttribute(t,e){switch(e){case Boolean:t=t?g:null;break;case Object:case Array:t=null==t?t:JSON.stringify(t)}return t},fromAttribute(t,e){let i=t;switch(e){case Boolean:i=null!==t;break;case Number:i=null===t?null:Number(t);break;case Object:case Array:try{i=JSON.parse(t)}catch(t){i=null}}return i}},y=(t,e)=>!c(t,e),w={attribute:!0,type:String,converter:f,reflect:!1,useDefault:!1,hasChanged:y};Symbol.metadata??=Symbol("metadata"),m.litPropertyMetadata??=new WeakMap;let $=class extends HTMLElement{static addInitializer(t){this._$Ei(),(this.l??=[]).push(t)}static get observedAttributes(){return this.finalize(),this._$Eh&&[...this._$Eh.keys()]}static createProperty(t,e=w){if(e.state&&(e.attribute=!1),this._$Ei(),this.prototype.hasOwnProperty(t)&&((e=Object.create(e)).wrapped=!0),this.elementProperties.set(t,e),!e.noAccessor){const i=Symbol(),s=this.getPropertyDescriptor(t,i,e);void 0!==s&&l(this.prototype,t,s)}}static getPropertyDescriptor(t,e,i){const{get:s,set:r}=h(this.prototype,t)??{get(){return this[e]},set(t){this[e]=t}};return{get:s,set(e){const a=s?.call(this);r?.call(this,e),this.requestUpdate(t,a,i)},configurable:!0,enumerable:!0}}static getPropertyOptions(t){return this.elementProperties.get(t)??w}static _$Ei(){if(this.hasOwnProperty(b("elementProperties")))return;const t=u(this);t.finalize(),void 0!==t.l&&(this.l=[...t.l]),this.elementProperties=new Map(t.elementProperties)}static finalize(){if(this.hasOwnProperty(b("finalized")))return;if(this.finalized=!0,this._$Ei(),this.hasOwnProperty(b("properties"))){const t=this.properties,e=[...d(t),...p(t)];for(const i of e)this.createProperty(i,t[i])}const t=this[Symbol.metadata];if(null!==t){const e=litPropertyMetadata.get(t);if(void 0!==e)for(const[t,i]of e)this.elementProperties.set(t,i)}this._$Eh=new Map;for(const[t,e]of this.elementProperties){const i=this._$Eu(t,e);void 0!==i&&this._$Eh.set(i,t)}this.elementStyles=this.finalizeStyles(this.styles)}static finalizeStyles(t){const e=[];if(Array.isArray(t)){const i=new Set(t.flat(1/0).reverse());for(const t of i)e.unshift(o(t))}else void 0!==t&&e.push(o(t));return e}static _$Eu(t,e){const i=e.attribute;return!1===i?void 0:"string"==typeof i?i:"string"==typeof t?t.toLowerCase():void 0}constructor(){super(),this._$Ep=void 0,this.isUpdatePending=!1,this.hasUpdated=!1,this._$Em=null,this._$Ev()}_$Ev(){this._$ES=new Promise(t=>this.enableUpdating=t),this._$AL=new Map,this._$E_(),this.requestUpdate(),this.constructor.l?.forEach(t=>t(this))}addController(t){(this._$EO??=new Set).add(t),void 0!==this.renderRoot&&this.isConnected&&t.hostConnected?.()}removeController(t){this._$EO?.delete(t)}_$E_(){const t=new Map,e=this.constructor.elementProperties;for(const i of e.keys())this.hasOwnProperty(i)&&(t.set(i,this[i]),delete this[i]);t.size>0&&(this._$Ep=t)}createRenderRoot(){const t=this.shadowRoot??this.attachShadow(this.constructor.shadowRootOptions);return((t,s)=>{if(i)t.adoptedStyleSheets=s.map(t=>t instanceof CSSStyleSheet?t:t.styleSheet);else for(const i of s){const s=document.createElement("style"),r=e.litNonce;void 0!==r&&s.setAttribute("nonce",r),s.textContent=i.cssText,t.appendChild(s)}})(t,this.constructor.elementStyles),t}connectedCallback(){this.renderRoot??=this.createRenderRoot(),this.enableUpdating(!0),this._$EO?.forEach(t=>t.hostConnected?.())}enableUpdating(t){}disconnectedCallback(){this._$EO?.forEach(t=>t.hostDisconnected?.())}attributeChangedCallback(t,e,i){this._$AK(t,i)}_$ET(t,e){const i=this.constructor.elementProperties.get(t),s=this.constructor._$Eu(t,i);if(void 0!==s&&!0===i.reflect){const r=(void 0!==i.converter?.toAttribute?i.converter:f).toAttribute(e,i.type);this._$Em=t,null==r?this.removeAttribute(s):this.setAttribute(s,r),this._$Em=null}}_$AK(t,e){const i=this.constructor,s=i._$Eh.get(t);if(void 0!==s&&this._$Em!==s){const t=i.getPropertyOptions(s),r="function"==typeof t.converter?{fromAttribute:t.converter}:void 0!==t.converter?.fromAttribute?t.converter:f;this._$Em=s;const a=r.fromAttribute(e,t.type);this[s]=a??this._$Ej?.get(s)??a,this._$Em=null}}requestUpdate(t,e,i,s=!1,r){if(void 0!==t){const a=this.constructor;if(!1===s&&(r=this[t]),i??=a.getPropertyOptions(t),!((i.hasChanged??y)(r,e)||i.useDefault&&i.reflect&&r===this._$Ej?.get(t)&&!this.hasAttribute(a._$Eu(t,i))))return;this.C(t,e,i)}!1===this.isUpdatePending&&(this._$ES=this._$EP())}C(t,e,{useDefault:i,reflect:s,wrapped:r},a){i&&!(this._$Ej??=new Map).has(t)&&(this._$Ej.set(t,a??e??this[t]),!0!==r||void 0!==a)||(this._$AL.has(t)||(this.hasUpdated||i||(e=void 0),this._$AL.set(t,e)),!0===s&&this._$Em!==t&&(this._$Eq??=new Set).add(t))}async _$EP(){this.isUpdatePending=!0;try{await this._$ES}catch(t){Promise.reject(t)}const t=this.scheduleUpdate();return null!=t&&await t,!this.isUpdatePending}scheduleUpdate(){return this.performUpdate()}performUpdate(){if(!this.isUpdatePending)return;if(!this.hasUpdated){if(this.renderRoot??=this.createRenderRoot(),this._$Ep){for(const[t,e]of this._$Ep)this[t]=e;this._$Ep=void 0}const t=this.constructor.elementProperties;if(t.size>0)for(const[e,i]of t){const{wrapped:t}=i,s=this[e];!0!==t||this._$AL.has(e)||void 0===s||this.C(e,void 0,i,s)}}let t=!1;const e=this._$AL;try{t=this.shouldUpdate(e),t?(this.willUpdate(e),this._$EO?.forEach(t=>t.hostUpdate?.()),this.update(e)):this._$EM()}catch(e){throw t=!1,this._$EM(),e}t&&this._$AE(e)}willUpdate(t){}_$AE(t){this._$EO?.forEach(t=>t.hostUpdated?.()),this.hasUpdated||(this.hasUpdated=!0,this.firstUpdated(t)),this.updated(t)}_$EM(){this._$AL=new Map,this.isUpdatePending=!1}get updateComplete(){return this.getUpdateComplete()}getUpdateComplete(){return this._$ES}shouldUpdate(t){return!0}update(t){this._$Eq&&=this._$Eq.forEach(t=>this._$ET(t,this[t])),this._$EM()}updated(t){}firstUpdated(t){}};$.elementStyles=[],$.shadowRootOptions={mode:"open"},$[b("elementProperties")]=new Map,$[b("finalized")]=new Map,v?.({ReactiveElement:$}),(m.reactiveElementVersions??=[]).push("2.1.2");const x=globalThis,k=t=>t,S=x.trustedTypes,A=S?S.createPolicy("lit-html",{createHTML:t=>t}):void 0,E="$lit$",C=`lit$${Math.random().toFixed(9).slice(2)}$`,P="?"+C,R=`<${P}>`,O=document,M=()=>O.createComment(""),N=t=>null===t||"object"!=typeof t&&"function"!=typeof t,U=Array.isArray,T="[ \t\n\f\r]",H=/<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g,z=/-->/g,L=/>/g,j=RegExp(`>|${T}(?:([^\\s"'>=/]+)(${T}*=${T}*(?:[^ \t\n\f\r"'\`<>=]|("|')|))|$)`,"g"),W=/'/g,I=/"/g,q=/^(?:script|style|textarea|title)$/i,D=(t=>(e,...i)=>({_$litType$:t,strings:e,values:i}))(1),F=Symbol.for("lit-noChange"),B=Symbol.for("lit-nothing"),V=new WeakMap,J=O.createTreeWalker(O,129);function K(t,e){if(!U(t)||!t.hasOwnProperty("raw"))throw Error("invalid template strings array");return void 0!==A?A.createHTML(e):e}const Y=(t,e)=>{const i=t.length-1,s=[];let r,a=2===e?"<svg>":3===e?"<math>":"",n=H;for(let e=0;e<i;e++){const i=t[e];let o,c,l=-1,h=0;for(;h<i.length&&(n.lastIndex=h,c=n.exec(i),null!==c);)h=n.lastIndex,n===H?"!--"===c[1]?n=z:void 0!==c[1]?n=L:void 0!==c[2]?(q.test(c[2])&&(r=RegExp("</"+c[2],"g")),n=j):void 0!==c[3]&&(n=j):n===j?">"===c[0]?(n=r??H,l=-1):void 0===c[1]?l=-2:(l=n.lastIndex-c[2].length,o=c[1],n=void 0===c[3]?j:'"'===c[3]?I:W):n===I||n===W?n=j:n===z||n===L?n=H:(n=j,r=void 0);const d=n===j&&t[e+1].startsWith("/>")?" ":"";a+=n===H?i+R:l>=0?(s.push(o),i.slice(0,l)+E+i.slice(l)+C+d):i+C+(-2===l?e:d)}return[K(t,a+(t[i]||"<?>")+(2===e?"</svg>":3===e?"</math>":"")),s]};class Z{constructor({strings:t,_$litType$:e},i){let s;this.parts=[];let r=0,a=0;const n=t.length-1,o=this.parts,[c,l]=Y(t,e);if(this.el=Z.createElement(c,i),J.currentNode=this.el.content,2===e||3===e){const t=this.el.content.firstChild;t.replaceWith(...t.childNodes)}for(;null!==(s=J.nextNode())&&o.length<n;){if(1===s.nodeType){if(s.hasAttributes())for(const t of s.getAttributeNames())if(t.endsWith(E)){const e=l[a++],i=s.getAttribute(t).split(C),n=/([.?@])?(.*)/.exec(e);o.push({type:1,index:r,name:n[2],strings:i,ctor:"."===n[1]?et:"?"===n[1]?it:"@"===n[1]?st:tt}),s.removeAttribute(t)}else t.startsWith(C)&&(o.push({type:6,index:r}),s.removeAttribute(t));if(q.test(s.tagName)){const t=s.textContent.split(C),e=t.length-1;if(e>0){s.textContent=S?S.emptyScript:"";for(let i=0;i<e;i++)s.append(t[i],M()),J.nextNode(),o.push({type:2,index:++r});s.append(t[e],M())}}}else if(8===s.nodeType)if(s.data===P)o.push({type:2,index:r});else{let t=-1;for(;-1!==(t=s.data.indexOf(C,t+1));)o.push({type:7,index:r}),t+=C.length-1}r++}}static createElement(t,e){const i=O.createElement("template");return i.innerHTML=t,i}}function G(t,e,i=t,s){if(e===F)return e;let r=void 0!==s?i._$Co?.[s]:i._$Cl;const a=N(e)?void 0:e._$litDirective$;return r?.constructor!==a&&(r?._$AO?.(!1),void 0===a?r=void 0:(r=new a(t),r._$AT(t,i,s)),void 0!==s?(i._$Co??=[])[s]=r:i._$Cl=r),void 0!==r&&(e=G(t,r._$AS(t,e.values),r,s)),e}class Q{constructor(t,e){this._$AV=[],this._$AN=void 0,this._$AD=t,this._$AM=e}get parentNode(){return this._$AM.parentNode}get _$AU(){return this._$AM._$AU}u(t){const{el:{content:e},parts:i}=this._$AD,s=(t?.creationScope??O).importNode(e,!0);J.currentNode=s;let r=J.nextNode(),a=0,n=0,o=i[0];for(;void 0!==o;){if(a===o.index){let e;2===o.type?e=new X(r,r.nextSibling,this,t):1===o.type?e=new o.ctor(r,o.name,o.strings,this,t):6===o.type&&(e=new rt(r,this,t)),this._$AV.push(e),o=i[++n]}a!==o?.index&&(r=J.nextNode(),a++)}return J.currentNode=O,s}p(t){let e=0;for(const i of this._$AV)void 0!==i&&(void 0!==i.strings?(i._$AI(t,i,e),e+=i.strings.length-2):i._$AI(t[e])),e++}}class X{get _$AU(){return this._$AM?._$AU??this._$Cv}constructor(t,e,i,s){this.type=2,this._$AH=B,this._$AN=void 0,this._$AA=t,this._$AB=e,this._$AM=i,this.options=s,this._$Cv=s?.isConnected??!0}get parentNode(){let t=this._$AA.parentNode;const e=this._$AM;return void 0!==e&&11===t?.nodeType&&(t=e.parentNode),t}get startNode(){return this._$AA}get endNode(){return this._$AB}_$AI(t,e=this){t=G(this,t,e),N(t)?t===B||null==t||""===t?(this._$AH!==B&&this._$AR(),this._$AH=B):t!==this._$AH&&t!==F&&this._(t):void 0!==t._$litType$?this.$(t):void 0!==t.nodeType?this.T(t):(t=>U(t)||"function"==typeof t?.[Symbol.iterator])(t)?this.k(t):this._(t)}O(t){return this._$AA.parentNode.insertBefore(t,this._$AB)}T(t){this._$AH!==t&&(this._$AR(),this._$AH=this.O(t))}_(t){this._$AH!==B&&N(this._$AH)?this._$AA.nextSibling.data=t:this.T(O.createTextNode(t)),this._$AH=t}$(t){const{values:e,_$litType$:i}=t,s="number"==typeof i?this._$AC(t):(void 0===i.el&&(i.el=Z.createElement(K(i.h,i.h[0]),this.options)),i);if(this._$AH?._$AD===s)this._$AH.p(e);else{const t=new Q(s,this),i=t.u(this.options);t.p(e),this.T(i),this._$AH=t}}_$AC(t){let e=V.get(t.strings);return void 0===e&&V.set(t.strings,e=new Z(t)),e}k(t){U(this._$AH)||(this._$AH=[],this._$AR());const e=this._$AH;let i,s=0;for(const r of t)s===e.length?e.push(i=new X(this.O(M()),this.O(M()),this,this.options)):i=e[s],i._$AI(r),s++;s<e.length&&(this._$AR(i&&i._$AB.nextSibling,s),e.length=s)}_$AR(t=this._$AA.nextSibling,e){for(this._$AP?.(!1,!0,e);t!==this._$AB;){const e=k(t).nextSibling;k(t).remove(),t=e}}setConnected(t){void 0===this._$AM&&(this._$Cv=t,this._$AP?.(t))}}class tt{get tagName(){return this.element.tagName}get _$AU(){return this._$AM._$AU}constructor(t,e,i,s,r){this.type=1,this._$AH=B,this._$AN=void 0,this.element=t,this.name=e,this._$AM=s,this.options=r,i.length>2||""!==i[0]||""!==i[1]?(this._$AH=Array(i.length-1).fill(new String),this.strings=i):this._$AH=B}_$AI(t,e=this,i,s){const r=this.strings;let a=!1;if(void 0===r)t=G(this,t,e,0),a=!N(t)||t!==this._$AH&&t!==F,a&&(this._$AH=t);else{const s=t;let n,o;for(t=r[0],n=0;n<r.length-1;n++)o=G(this,s[i+n],e,n),o===F&&(o=this._$AH[n]),a||=!N(o)||o!==this._$AH[n],o===B?t=B:t!==B&&(t+=(o??"")+r[n+1]),this._$AH[n]=o}a&&!s&&this.j(t)}j(t){t===B?this.element.removeAttribute(this.name):this.element.setAttribute(this.name,t??"")}}class et extends tt{constructor(){super(...arguments),this.type=3}j(t){this.element[this.name]=t===B?void 0:t}}class it extends tt{constructor(){super(...arguments),this.type=4}j(t){this.element.toggleAttribute(this.name,!!t&&t!==B)}}class st extends tt{constructor(t,e,i,s,r){super(t,e,i,s,r),this.type=5}_$AI(t,e=this){if((t=G(this,t,e,0)??B)===F)return;const i=this._$AH,s=t===B&&i!==B||t.capture!==i.capture||t.once!==i.once||t.passive!==i.passive,r=t!==B&&(i===B||s);s&&this.element.removeEventListener(this.name,this,i),r&&this.element.addEventListener(this.name,this,t),this._$AH=t}handleEvent(t){"function"==typeof this._$AH?this._$AH.call(this.options?.host??this.element,t):this._$AH.handleEvent(t)}}class rt{constructor(t,e,i){this.element=t,this.type=6,this._$AN=void 0,this._$AM=e,this.options=i}get _$AU(){return this._$AM._$AU}_$AI(t){G(this,t)}}const at=x.litHtmlPolyfillSupport;at?.(Z,X),(x.litHtmlVersions??=[]).push("3.3.3");const nt=globalThis;let ot=class extends ${constructor(){super(...arguments),this.renderOptions={host:this},this._$Do=void 0}createRenderRoot(){const t=super.createRenderRoot();return this.renderOptions.renderBefore??=t.firstChild,t}update(t){const e=this.render();this.hasUpdated||(this.renderOptions.isConnected=this.isConnected),super.update(t),this._$Do=((t,e,i)=>{const s=i?.renderBefore??e;let r=s._$litPart$;if(void 0===r){const t=i?.renderBefore??null;s._$litPart$=r=new X(e.insertBefore(M(),t),t,void 0,i??{})}return r._$AI(t),r})(e,this.renderRoot,this.renderOptions)}connectedCallback(){super.connectedCallback(),this._$Do?.setConnected(!0)}disconnectedCallback(){super.disconnectedCallback(),this._$Do?.setConnected(!1)}render(){return F}};ot._$litElement$=!0,ot.finalized=!0,nt.litElementHydrateSupport?.({LitElement:ot});const ct=nt.litElementPolyfillSupport;ct?.({LitElement:ot}),(nt.litElementVersions??=[]).push("4.2.2");const lt=t=>(e,i)=>{void 0!==i?i.addInitializer(()=>{customElements.define(t,e)}):customElements.define(t,e)},ht={attribute:!0,type:String,converter:f,reflect:!1,hasChanged:y},dt=(t=ht,e,i)=>{const{kind:s,metadata:r}=i;let a=globalThis.litPropertyMetadata.get(r);if(void 0===a&&globalThis.litPropertyMetadata.set(r,a=new Map),"setter"===s&&((t=Object.create(t)).wrapped=!0),a.set(i.name,t),"accessor"===s){const{name:s}=i;return{set(i){const r=e.get.call(this);e.set.call(this,i),this.requestUpdate(s,r,t,!0,i)},init(e){return void 0!==e&&this.C(s,void 0,t,e),e}}}if("setter"===s){const{name:s}=i;return function(i){const r=this[s];e.call(this,i),this.requestUpdate(s,r,t,!0,i)}}throw Error("Unsupported decorator location: "+s)};function pt(t){return(e,i)=>"object"==typeof i?dt(t,e,i):((t,e,i)=>{const s=e.hasOwnProperty(i);return e.constructor.createProperty(i,t),s?Object.getOwnPropertyDescriptor(e,i):void 0})(t,e,i)}function ut(t){return pt({...t,state:!0,attribute:!1})}let mt=class{constructor(t){}get _$AU(){return this._$AM._$AU}_$AT(t,e,i){this._$Ct=t,this._$AM=e,this._$Ci=i}_$AS(t,e){return this.update(t,e)}update(t,e){return this.render(...e)}};const _t={},gt=(t=>(...e)=>({_$litDirective$:t,values:e}))(class extends mt{constructor(){super(...arguments),this.key=B}render(t,e){return this.key=t,e}update(t,[e,i]){return e!==this.key&&(((t,e=_t)=>{t._$AH=e})(t),this.key=e),i}});function vt(t,e,i){for(const s of e){const[e]=s.split(".",1),r=s.split(".")[1]??"",a=i.states[s]?.attributes.device_class;"sensor"===e?"temperature"===a?t.temperature=s:"humidity"===a?t.humidity=s:"illuminance"===a&&(t.light=s):"binary_sensor"===e?"motion"===a||r.endsWith("_motion")||r.endsWith("_cloud_motion")?t.motion=s:("sound"===a||r.endsWith("_sound")||r.endsWith("_cloud_sound"))&&(t.sound=s):"switch"===e&&r.endsWith("_camera_power")?t.power=s:"light"===e&&r.endsWith("_night_light")&&!r.includes("sl_")?t.night_light=s:"media_player"===e&&r.endsWith("_sound_machine")&&(t.sound_machine=s)}}function bt(t,e){if(!e)return!1;const i=t.entities[e];return!i?.disabled_by&&e in t.states}const ft=n`
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
    padding: 10px 16px 6px;
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

  .header-actions {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .wifi-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(50, 160, 200, 0.2);
    border: none;
    padding: 8px;
    border-radius: 50%;
    cursor: pointer;
    color: var(--nanit-teal);
    transition: background var(--nanit-transition),
                box-shadow var(--nanit-transition);
    box-shadow: 0 0 8px var(--nanit-teal-glow);
  }

  .wifi-btn:hover {
    background: rgba(50, 160, 200, 0.3);
    box-shadow: 0 0 14px var(--nanit-teal-glow);
  }

  .wifi-btn ha-icon {
    --mdc-icon-size: 24px;
  }

  /* -- Network Popup -- */

  .network-backdrop {
    position: fixed;
    inset: 0;
    z-index: 99;
  }

  .network-popup {
    position: absolute;
    top: 52px;
    right: 8px;
    z-index: 100;
    background: var(--ha-card-background, var(--card-background-color));
    border: 1px solid rgba(50, 160, 200, 0.3);
    border-radius: var(--nanit-radius);
    padding: 14px;
    min-width: 220px;
    box-shadow: 0 4px 24px rgba(0, 0, 0, 0.25),
                0 0 12px var(--nanit-teal-glow);
    animation: popupIn 0.2s ease;
  }

  @keyframes popupIn {
    from {
      opacity: 0;
      transform: translateY(-8px) scale(0.96);
    }
    to {
      opacity: 1;
      transform: translateY(0) scale(1);
    }
  }

  .network-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 12px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--divider-color, rgba(127, 127, 127, 0.15));
    color: var(--nanit-teal);
    font-size: 14px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .network-header ha-icon {
    --mdc-icon-size: 20px;
  }

  .network-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 0;
  }

  .network-row + .network-row {
    border-top: 1px solid var(--divider-color, rgba(127, 127, 127, 0.08));
  }

  .network-row > ha-icon {
    --mdc-icon-size: 20px;
    color: var(--nanit-teal);
    flex-shrink: 0;
  }

  .network-detail {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .network-label {
    font-size: 11px;
    font-weight: 500;
    color: var(--secondary-text-color);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .network-value {
    font-size: 14px;
    font-weight: 500;
    color: var(--primary-text-color);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
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
    margin: 0 4px;
    aspect-ratio: 16 / 9;
    min-height: 180px;
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

  /* -- Stream Loading Overlay -- */

  .stream-loader {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #000;
    z-index: 3;
    transition: opacity 0.6s ease;
    pointer-events: none;
  }

  .stream-loader.hidden {
    opacity: 0;
  }

  .loader-content {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 16px;
  }

  .loader-content ha-icon {
    --mdc-icon-size: 36px;
    color: var(--nanit-amber);
    opacity: 0.7;
  }

  .loader-spinner {
    width: 28px;
    height: 28px;
    border: 3px solid rgba(201, 168, 76, 0.2);
    border-top-color: var(--nanit-amber);
    border-radius: 50%;
    animation: spin 1s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
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
    padding: var(--nanit-gap) 4px 4px;
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
`;let yt=class extends ot{setConfig(t){this._config={...t}}_entityChanged(t,e){const i=e.detail.value||void 0;this._config&&i!==this._config[t]&&this._updateConfig({[t]:i})}_toggleChanged(t,e){const i=e.target.checked;this._updateConfig({[t]:i})}_updateConfig(t){const e={...this._config,...t};for(const[i,s]of Object.entries(t))void 0===s&&delete e[i];this._config=e,this.dispatchEvent(new CustomEvent("config-changed",{bubbles:!0,composed:!0,detail:{config:e}}))}render(){return this.hass&&this._config?D`
      <div class="editor">
        <ha-entity-picker
          .hass=${this.hass}
          .value=${this._config.camera_entity_id||""}
          .includeDomains=${["camera"]}
          .label=${"Camera Entity"}
          allow-custom-entity
          @value-changed=${t=>this._entityChanged("camera_entity_id",t)}
        ></ha-entity-picker>
        <label class="toggle-row">
          <span>Hide baby name</span>
          <ha-switch
            .checked=${!0===this._config.hide_baby_name}
            @change=${t=>this._toggleChanged("hide_baby_name",t)}
          ></ha-switch>
        </label>
        <label class="toggle-row">
          <span>Hide connectivity status</span>
          <ha-switch
            .checked=${!0===this._config.hide_connectivity_status}
            @change=${t=>this._toggleChanged("hide_connectivity_status",t)}
          ></ha-switch>
        </label>
        <label class="toggle-row">
          <span>Hide power button</span>
          <ha-switch
            .checked=${!0===this._config.hide_power_button}
            @change=${t=>this._toggleChanged("hide_power_button",t)}
          ></ha-switch>
        </label>
        <label class="toggle-row">
          <span>Hide night light controls</span>
          <ha-switch
            .checked=${!0===this._config.hide_night_light}
            @change=${t=>this._toggleChanged("hide_night_light",t)}
          ></ha-switch>
        </label>
        <label class="toggle-row">
          <span>Hide sound machine controls</span>
          <ha-switch
            .checked=${!0===this._config.hide_sound_machine}
            @change=${t=>this._toggleChanged("hide_sound_machine",t)}
          ></ha-switch>
        </label>
        <ha-entity-picker
          .hass=${this.hass}
          .value=${this._config.temperature_entity_id||""}
          .includeDomains=${["sensor"]}
          .label=${"Temperature Entity Override"}
          allow-custom-entity
          @value-changed=${t=>this._entityChanged("temperature_entity_id",t)}
        ></ha-entity-picker>
        <ha-entity-picker
          .hass=${this.hass}
          .value=${this._config.humidity_entity_id||""}
          .includeDomains=${["sensor"]}
          .label=${"Humidity Entity Override"}
          allow-custom-entity
          @value-changed=${t=>this._entityChanged("humidity_entity_id",t)}
        ></ha-entity-picker>
      </div>
    `:B}};yt.styles=n`
    .editor {
      padding: 16px;
    }
    ha-entity-picker {
      display: block;
    }
    .toggle-row {
      align-items: center;
      display: flex;
      justify-content: space-between;
      padding-top: 16px;
    }
  `,t([pt({attribute:!1})],yt.prototype,"hass",void 0),t([ut()],yt.prototype,"_config",void 0),yt=t([lt("nanit-card-editor")],yt);const wt="video, img, canvas, ha-hls-player, ha-web-rtc-player";let $t=class extends ot{constructor(){super(...arguments),this._streamLoaded=!1,this._streamHealthy=!1,this._showNetwork=!1,this._streamEpoch=0,this._lastVideoTime=0,this._sawProgress=!1,this._stallStrikes=0,this._startupStrikes=0,this._healthyTicks=0,this._reloadCount=0,this._reloadWindowStart=0,this._cooldownUntil=0,this._streamMountedAt=0,this._watchedEpoch=-1,this._recoveringStream=!1,this._resumeRecoverUntil=0,this._resumeProbeUntil=0,this._backendRecoveryInFlight=!1,this._lastBackendResetAt=0,this._recoverStreamOnResume=()=>{if(document.hidden)return;const t=this._entities();if(!this._isCameraOn(t))return;const e=this.renderRoot.querySelector("ha-camera-stream");if(!e)return;const i=this._findStreamVideo(e);i?.paused&&i.play().catch(()=>{});const s=Date.now();s<this._resumeRecoverUntil||(this._resumeRecoverUntil=s+1e4,this._startupStrikes=0,this._stallStrikes=0,this._healthyTicks=0,i&&(this._lastVideoTime=i.currentTime),this._resumeProbeUntil=s+6e3,this._checkStreamLiveness())}}static getConfigElement(){return document.createElement("nanit-card-editor")}static getStubConfig(t){const e=Object.keys(t.states).find(e=>e.startsWith("camera.")&&"nanit"===t.entities[e]?.platform);return{type:"custom:nanit-card",camera_entity_id:e||""}}setConfig(t){if(!t)throw new Error("Invalid configuration");this._config=t}getCardSize(){return 5}_entities(){return this._config?.camera_entity_id&&this.hass?function(t,e){const i={};i.camera=e;const s=t.entities[e];if(s?.device_id){const e=s.device_id,r=[],a=[];for(const i of Object.keys(t.entities))t.entities[i].device_id===e&&("diagnostic"===t.entities[i].entity_category?a.push(i):r.push(i));vt(i,r,t),function(t,e,i){for(const s of e){const[e]=s.split(".",1),r=s.split(".")[1]??"",a=i.states[s]?.attributes.device_class;"sensor"===e&&("signal_strength"===a?t.wifi_signal=s:"frequency"===a?t.wifi_frequency=s:r.endsWith("_wifi_ssid")&&(t.wifi_ssid=s))}}(i,a,t)}else{const s=e.split(".")[1]??"";vt(i,Object.keys(t.states).filter(t=>t!==e&&t.split(".")[1]?.startsWith(s.split("_camera")[0]||s)),t)}return i}(this.hass,this._config.camera_entity_id):{}}_isCameraOn(t){return!t.power||"off"!==this.hass.states[t.power]?.state}_fireMoreInfo(t){this.dispatchEvent(new CustomEvent("hass-more-info",{bubbles:!0,composed:!0,detail:{entityId:t}}))}_toggleService(t,e,i){this.hass.callService(t,e,{entity_id:i})}connectedCallback(){super.connectedCallback(),document.addEventListener("visibilitychange",this._recoverStreamOnResume),window.addEventListener("pageshow",this._recoverStreamOnResume)}disconnectedCallback(){super.disconnectedCallback(),document.removeEventListener("visibilitychange",this._recoverStreamOnResume),window.removeEventListener("pageshow",this._recoverStreamOnResume),this._clearStreamWatchdog(),this._clearBackendRecoveryFallback()}updated(t){super.updated(t);this.renderRoot.querySelector("ha-camera-stream")?(this._watchedEpoch!==this._streamEpoch&&(this._watchedEpoch=this._streamEpoch,this._streamMountedAt=Date.now()),this._streamWatchdog||this._startStreamWatchdog()):this._clearStreamWatchdog()}_startStreamWatchdog(){this._streamWatchdog=setInterval(()=>this._checkStreamLiveness(),1e3),this._checkStreamLiveness()}_clearStreamWatchdog(){this._streamWatchdog&&(clearInterval(this._streamWatchdog),this._streamWatchdog=void 0)}_hasVisualStreamElement(t){const e=t.shadowRoot;if(!e)return!1;if(e.querySelector(wt))return!0;for(const t of Array.from(e.querySelectorAll("*")))if(t.shadowRoot?.querySelector(wt))return!0;return!1}_findStreamVideo(t){const e=t.shadowRoot;if(!e)return null;const i=e.querySelector("video");if(i)return i;const s=e.querySelector("ha-hls-player, ha-web-rtc-player"),r=s?.shadowRoot?.querySelector("video");if(r)return r;for(const t of Array.from(e.querySelectorAll("*"))){const e=t.shadowRoot?.querySelector("video");if(e)return e}return null}_strikeThreshold(t){return Date.now()<this._resumeProbeUntil?Math.min(t,3):t}_checkStreamLiveness(){const t=this.renderRoot.querySelector("ha-camera-stream");if(!t)return;const e=this._hasVisualStreamElement(t),i=this._findStreamVideo(t);if(!this._streamLoaded&&this._streamMountedAt>0&&Date.now()-this._streamMountedAt>3500&&e&&(this._streamLoaded=!0),!i){this._startupStrikes+=1;const t=e?30:15;return void(this._startupStrikes>=this._strikeThreshold(t)&&this._recoverStream())}return!this._streamLoaded&&i.readyState>=2&&(this._streamLoaded=!0),i.readyState<2?(this._startupStrikes+=1,void(this._startupStrikes>=this._strikeThreshold(15)&&this._recoverStream())):i.currentTime>this._lastVideoTime+.05?(this._lastVideoTime=i.currentTime,this._sawProgress=!0,this._stallStrikes=0,this._startupStrikes=0,this._streamLoaded=!0,this._streamHealthy=!0,this._clearBackendRecoveryFallback(),void(!i.paused&&++this._healthyTicks>=10&&(this._reloadCount=0,this._cooldownUntil=0,this._reloadWindowStart=0))):(this._healthyTicks=0,this._streamHealthy=!1,this._sawProgress?(this._stallStrikes+=1,void(this._stallStrikes>=this._strikeThreshold(8)&&this._recoverStream())):(this._startupStrikes+=1,void(this._startupStrikes>=this._strikeThreshold(15)&&this._recoverStream())))}_requestBackendStreamReset(){const t=this._entities();return t.camera?this.hass.callService("nanit","reset_stream",{entity_id:t.camera}):Promise.resolve()}_clearBackendRecoveryFallback(){void 0!==this._backendRecoveryFallback&&(window.clearTimeout(this._backendRecoveryFallback),this._backendRecoveryFallback=void 0)}_scheduleBackendRecoveryFallback(){this._clearBackendRecoveryFallback(),this._backendRecoveryFallback=window.setTimeout(()=>{if(this._backendRecoveryFallback=void 0,this._streamHealthy||this._backendRecoveryInFlight)return;const t=Date.now();t-this._lastBackendResetAt<45e3?this._reloadStream():(this._lastBackendResetAt=t,this._backendRecoveryInFlight=!0,this._requestBackendStreamReset().catch(()=>{}).finally(()=>{this._backendRecoveryInFlight=!1,this._reloadStream()}))},2e4)}async _recoverStream(){if(!this._recoveringStream){this._recoveringStream=!0;try{this._reloadStream(),this._scheduleBackendRecoveryFallback()}finally{this._recoveringStream=!1}}}_reloadStream(){const t=Date.now();t<this._cooldownUntil?this._stallStrikes=0:(t-this._reloadWindowStart>6e4&&(this._reloadWindowStart=t,this._reloadCount=0),this._reloadCount+=1,this._reloadCount>=3&&(this._cooldownUntil=t+6e4),this._streamEpoch+=1,this._streamLoaded=!1,this._streamHealthy=!1,this._lastVideoTime=0,this._sawProgress=!1,this._stallStrikes=0,this._startupStrikes=0,this._healthyTicks=0,this._resumeProbeUntil=0)}_resetStreamState(){this._streamLoaded=!1,this._streamHealthy=!1,this._clearStreamWatchdog(),this._clearBackendRecoveryFallback(),this._lastVideoTime=0,this._sawProgress=!1,this._stallStrikes=0,this._startupStrikes=0,this._healthyTicks=0,this._reloadWindowStart=0,this._resumeRecoverUntil=0,this._resumeProbeUntil=0,this._streamMountedAt=0,this._watchedEpoch=-1}render(){if(!this.hass||!this._config)return D`<ha-card><div class="header"><span class="device-name">Nanit</span></div></ha-card>`;const t=this._entities(),e=this._isCameraOn(t);e||this._resetStreamState();const i=t.camera?function(t,e){const i=t.states[e];return i?(i.attributes.friendly_name??"Nanit").replace(/ Camera$/i,""):"Nanit"}(this.hass,t.camera):"Nanit";return D`
      <ha-card>
        ${this._renderHeader(i,t,e)}
        <div class="card-content ${e?"":"collapsed"}">
          ${e?this._renderStream(t):B}
          ${e?this._renderControls(t):B}
        </div>
      </ha-card>
    `}_renderHeader(t,e,i){const s=!this._config.hide_connectivity_status&&(bt(this.hass,e.wifi_ssid)||bt(this.hass,e.wifi_signal)||bt(this.hass,e.wifi_frequency)),r=!this._config.hide_baby_name,a=e.power&&!this._config.hide_power_button;return r||!i||s||a?D`
      <div class="header">
        ${r?D`
              <div class="device-badge">
                <ha-icon icon="mdi:baby-face-outline"></ha-icon>
                <span class="device-name">${t}</span>
              </div>
            `:B}
        ${i?B:D`<span class="camera-off-label">Camera Off</span>`}
        <div class="header-actions">
          ${s?D`
                <button
                  class="wifi-btn"
                  @click=${()=>{this._showNetwork=!this._showNetwork}}
                >
                  <ha-icon icon="mdi:wifi"></ha-icon>
                </button>
              `:B}
          ${a?D`
                <button
                  class="power-btn ${i?"":"off"}"
                  @click=${()=>this._toggleService("switch","toggle",e.power)}
                >
                  <ha-icon icon="mdi:power"></ha-icon>
                </button>
              `:B}
        </div>
      </div>
      ${this._showNetwork?this._renderNetworkPopup(e):B}
    `:D``}_renderNetworkPopup(t){const e=t.wifi_ssid?this.hass.states[t.wifi_ssid]?.state:void 0,i=t.wifi_signal?this.hass.states[t.wifi_signal]?.state:void 0,s=t.wifi_signal?this.hass.states[t.wifi_signal]?.attributes.unit_of_measurement??"dBm":"dBm",r=t.wifi_frequency?this.hass.states[t.wifi_frequency]?.state:void 0,a=t.wifi_frequency?this.hass.states[t.wifi_frequency]?.attributes.unit_of_measurement??"MHz":"MHz",n=i?parseInt(i,10):-100;let o="Weak",c="#e74c3c";return n>=-50?(o="Excellent",c="#2ecc71"):n>=-60?(o="Good",c="var(--nanit-teal)"):n>=-70&&(o="Fair",c="var(--nanit-amber)"),D`
      <div class="network-backdrop" @click=${()=>{this._showNetwork=!1}}></div>
      <div class="network-popup">
        <div class="network-header">
          <ha-icon icon="mdi:wifi"></ha-icon>
          <span>Network</span>
        </div>
        ${e?D`
              <div class="network-row">
                <ha-icon icon="mdi:router-wireless"></ha-icon>
                <div class="network-detail">
                  <span class="network-label">WiFi Name</span>
                  <span class="network-value">${e}</span>
                </div>
              </div>
            `:B}
        ${i?D`
              <div class="network-row">
                <ha-icon icon="mdi:signal" style="color: ${c}"></ha-icon>
                <div class="network-detail">
                  <span class="network-label">Signal Strength</span>
                  <span class="network-value">${i} ${s} · <span style="color: ${c}">${o}</span></span>
                </div>
              </div>
            `:B}
        ${r?D`
              <div class="network-row">
                <ha-icon icon="mdi:frequency"></ha-icon>
                <div class="network-detail">
                  <span class="network-label">Frequency</span>
                  <span class="network-value">${r} ${a}</span>
                </div>
              </div>
            `:B}
      </div>
    `}_renderStream(t){const e=t.camera?this.hass.states[t.camera]:void 0;return D`
      <div class="stream-wrap">
        ${e?D`
              <div
                class="stream-click"
                @click=${()=>t.camera&&this._fireMoreInfo(t.camera)}
              >
                ${gt(`${t.camera}-${this._streamEpoch}`,D`
                  <ha-camera-stream
                    muted
                    data-stream-epoch=${this._streamEpoch}
                    .hass=${this.hass}
                    .stateObj=${e}

                  ></ha-camera-stream>
                `)}
              </div>
              <div class="stream-loader ${this._streamLoaded?"hidden":""}">
                <div class="loader-content">
                  <ha-icon icon="mdi:camera"></ha-icon>
                  <div class="loader-spinner"></div>
                </div>
              </div>
            `:D`
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
    `}_renderSensorOverlays(t){const e=[],i=this._config.temperature_entity_id||t.temperature,s=this._config.humidity_entity_id||t.humidity;if(bt(this.hass,i)){const t=parseFloat(this.hass.states[i].state),s=isNaN(t)?this.hass.states[i].state:t.toFixed(1),r=this.hass.states[i].attributes.unit_of_measurement??"";e.push(D`
        <div class="pill pill-temp" @click=${()=>this._fireMoreInfo(i)}>
          <ha-icon icon="mdi:thermometer"></ha-icon>
          <span>${s}${r}</span>
        </div>
      `)}if(bt(this.hass,s)){const t=parseFloat(this.hass.states[s].state),i=isNaN(t)?this.hass.states[s].state:t.toFixed(1),r=this.hass.states[s].attributes.unit_of_measurement??"%";e.push(D`
        <div class="pill pill-humid" @click=${()=>this._fireMoreInfo(s)}>
          <ha-icon icon="mdi:water-percent"></ha-icon>
          <span>${i}${r}</span>
        </div>
      `)}return 0===e.length?D``:D`<div class="overlay-top">${e}</div>`}_renderDetectionOverlays(t){const e=bt(this.hass,t.motion),i=bt(this.hass,t.sound);if(!e&&!i)return D``;const s=e&&"on"===this.hass.states[t.motion].state,r=i&&"on"===this.hass.states[t.sound].state;return D`
      <div class="overlay-bottom">
        ${e?D`
              <div
                class="pill ${s?"active motion-active":""}"
                @click=${()=>this._fireMoreInfo(t.motion)}
              >
                <ha-icon icon="mdi:motion-sensor"></ha-icon>
                <span>${s?"Motion":"Clear"}</span>
              </div>
            `:D`<div></div>`}
        ${i?D`
              <div
                class="pill ${r?"active sound-active":""}"
                @click=${()=>this._fireMoreInfo(t.sound)}
              >
                <ha-icon icon="mdi:ear-hearing"></ha-icon>
                <span>${r?"Sound":"Quiet"}</span>
              </div>
            `:B}
      </div>
    `}_renderControls(t){const e=!this._config.hide_night_light&&bt(this.hass,t.night_light),i=!this._config.hide_sound_machine&&bt(this.hass,t.sound_machine);return e||i?D`
      <div class="controls">
        ${e?this._renderNightLight(t.night_light):B}
        ${i?this._renderSoundMachine(t.sound_machine):B}
      </div>
    `:D``}_renderNightLight(t){const e=this.hass.states[t],i="on"===e?.state,s=e?.attributes.brightness??0,r=Math.round(s/255*100);return D`
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
            <div class="nanit-slider" style="--slider-pct: ${r}%">
              <input
                type="range"
                min="0"
                max="100"
                .value=${String(r)}
                @input=${t=>{const e=t.target.closest(".nanit-slider");e&&e.style.setProperty("--slider-pct",`${t.target.value}%`)}}
                @change=${e=>{const i=Number(e.target.value);0===i?this.hass.callService("light","turn_off",{entity_id:t}):this.hass.callService("light","turn_on",{entity_id:t,brightness:Math.round(i/100*255)})}}
              />
            </div>
          </div>
        </div>
      </div>
    `}_renderSoundMachine(t){const e=this.hass.states[t],i="playing"===e?.state,s=e?.attributes.source??"",r=e?.attributes.source_list??[],a=e?.attributes.volume_level??0,n=Math.round(100*a);return D`
      <div class="control-section control-section-sound">
        <div class="section-header">
          <span class="control-label">Sound Machine</span>
          ${r.length>0?D`
                <div class="source-list">
                  ${r.map(e=>D`
                      <button
                        class="source-icon ${e===s?"active":""}"
                        title=${this._formatSourceName(e)}
                        @click=${()=>this.hass.callService("media_player","select_source",{entity_id:t,source:e})}
                      >
                        <ha-icon icon=${this._sourceIcon(e)}></ha-icon>
                      </button>
                    `)}
                </div>
              `:B}
        </div>
        <div class="control-row">
          <button
            class="icon-btn ${i?"active":""}"
            @click=${()=>this._toggleService("media_player",i?"media_stop":"media_play",t)}
          >
            <ha-icon icon="mdi:${i?"stop":"play"}"></ha-icon>
          </button>
          ${i?D`<span class="track-name">${this._formatSourceName(s)}</span>`:B}
          <div class="slider-row">
            <div class="nanit-slider" style="--slider-pct: ${n}%">
              <input
                type="range"
                min="0"
                max="100"
                .value=${String(n)}
                @input=${t=>{const e=t.target.closest(".nanit-slider");e&&e.style.setProperty("--slider-pct",`${t.target.value}%`)}}
                @change=${e=>{const i=Number(e.target.value);this.hass.callService("media_player","volume_set",{entity_id:t,volume_level:i/100})}}
              />
            </div>
          </div>
        </div>
      </div>
    `}_sourceIcon(t){return{white_noise:"mdi:sine-wave",birds:"mdi:bird",waves:"mdi:waves",wind:"mdi:weather-windy",rain:"mdi:weather-rainy",water_stream:"mdi:water",fan:"mdi:fan",heartbeat:"mdi:heart-pulse",dryer:"mdi:tumble-dryer",vacuum:"mdi:robot-vacuum"}[t.replace(/\.wav$/i,"").toLowerCase()]??"mdi:music-note"}_formatSourceName(t){return t.replace(/\.wav$/i,"").replace(/_/g," ")}};$t.styles=ft,t([pt({attribute:!1})],$t.prototype,"hass",void 0),t([ut()],$t.prototype,"_config",void 0),t([ut()],$t.prototype,"_streamLoaded",void 0),t([ut()],$t.prototype,"_showNetwork",void 0),t([ut()],$t.prototype,"_streamEpoch",void 0),$t=t([lt("nanit-card")],$t),window.customCards=window.customCards||[],window.customCards.push({type:"nanit-card",name:"Nanit Camera",description:"Camera stream with controls for Nanit baby cameras",preview:!0});export{$t as NanitCard};
