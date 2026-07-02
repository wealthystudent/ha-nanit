function t(t,e,i,s){var n,r=arguments.length,a=r<3?e:null===s?s=Object.getOwnPropertyDescriptor(e,i):s;if("object"==typeof Reflect&&"function"==typeof Reflect.decorate)a=Reflect.decorate(t,e,i,s);else for(var o=t.length-1;o>=0;o--)(n=t[o])&&(a=(r<3?n(a):r>3?n(e,i,a):n(e,i))||a);return r>3&&a&&Object.defineProperty(e,i,a),a}"function"==typeof SuppressedError&&SuppressedError;const e=globalThis,i=e.ShadowRoot&&(void 0===e.ShadyCSS||e.ShadyCSS.nativeShadow)&&"adoptedStyleSheets"in Document.prototype&&"replace"in CSSStyleSheet.prototype,s=Symbol(),n=new WeakMap;let r=class{constructor(t,e,i){if(this._$cssResult$=!0,i!==s)throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");this.cssText=t,this.t=e}get styleSheet(){let t=this.o;const e=this.t;if(i&&void 0===t){const i=void 0!==e&&1===e.length;i&&(t=n.get(e)),void 0===t&&((this.o=t=new CSSStyleSheet).replaceSync(this.cssText),i&&n.set(e,t))}return t}toString(){return this.cssText}};const a=(t,...e)=>{const i=1===t.length?t[0]:e.reduce((e,i,s)=>e+(t=>{if(!0===t._$cssResult$)return t.cssText;if("number"==typeof t)return t;throw Error("Value passed to 'css' function must be a 'css' function result: "+t+". Use 'unsafeCSS' to pass non-literal values, but take care to ensure page security.")})(i)+t[s+1],t[0]);return new r(i,t,s)},o=i?t=>t:t=>t instanceof CSSStyleSheet?(t=>{let e="";for(const i of t.cssRules)e+=i.cssText;return(t=>new r("string"==typeof t?t:t+"",void 0,s))(e)})(t):t,{is:c,defineProperty:l,getOwnPropertyDescriptor:d,getOwnPropertyNames:h,getOwnPropertySymbols:p,getPrototypeOf:u}=Object,g=globalThis,m=g.trustedTypes,v=m?m.emptyScript:"",b=g.reactiveElementPolyfillSupport,_=(t,e)=>t,f={toAttribute(t,e){switch(e){case Boolean:t=t?v:null;break;case Object:case Array:t=null==t?t:JSON.stringify(t)}return t},fromAttribute(t,e){let i=t;switch(e){case Boolean:i=null!==t;break;case Number:i=null===t?null:Number(t);break;case Object:case Array:try{i=JSON.parse(t)}catch(t){i=null}}return i}},y=(t,e)=>!c(t,e),w={attribute:!0,type:String,converter:f,reflect:!1,useDefault:!1,hasChanged:y};Symbol.metadata??=Symbol("metadata"),g.litPropertyMetadata??=new WeakMap;let $=class extends HTMLElement{static addInitializer(t){this._$Ei(),(this.l??=[]).push(t)}static get observedAttributes(){return this.finalize(),this._$Eh&&[...this._$Eh.keys()]}static createProperty(t,e=w){if(e.state&&(e.attribute=!1),this._$Ei(),this.prototype.hasOwnProperty(t)&&((e=Object.create(e)).wrapped=!0),this.elementProperties.set(t,e),!e.noAccessor){const i=Symbol(),s=this.getPropertyDescriptor(t,i,e);void 0!==s&&l(this.prototype,t,s)}}static getPropertyDescriptor(t,e,i){const{get:s,set:n}=d(this.prototype,t)??{get(){return this[e]},set(t){this[e]=t}};return{get:s,set(e){const r=s?.call(this);n?.call(this,e),this.requestUpdate(t,r,i)},configurable:!0,enumerable:!0}}static getPropertyOptions(t){return this.elementProperties.get(t)??w}static _$Ei(){if(this.hasOwnProperty(_("elementProperties")))return;const t=u(this);t.finalize(),void 0!==t.l&&(this.l=[...t.l]),this.elementProperties=new Map(t.elementProperties)}static finalize(){if(this.hasOwnProperty(_("finalized")))return;if(this.finalized=!0,this._$Ei(),this.hasOwnProperty(_("properties"))){const t=this.properties,e=[...h(t),...p(t)];for(const i of e)this.createProperty(i,t[i])}const t=this[Symbol.metadata];if(null!==t){const e=litPropertyMetadata.get(t);if(void 0!==e)for(const[t,i]of e)this.elementProperties.set(t,i)}this._$Eh=new Map;for(const[t,e]of this.elementProperties){const i=this._$Eu(t,e);void 0!==i&&this._$Eh.set(i,t)}this.elementStyles=this.finalizeStyles(this.styles)}static finalizeStyles(t){const e=[];if(Array.isArray(t)){const i=new Set(t.flat(1/0).reverse());for(const t of i)e.unshift(o(t))}else void 0!==t&&e.push(o(t));return e}static _$Eu(t,e){const i=e.attribute;return!1===i?void 0:"string"==typeof i?i:"string"==typeof t?t.toLowerCase():void 0}constructor(){super(),this._$Ep=void 0,this.isUpdatePending=!1,this.hasUpdated=!1,this._$Em=null,this._$Ev()}_$Ev(){this._$ES=new Promise(t=>this.enableUpdating=t),this._$AL=new Map,this._$E_(),this.requestUpdate(),this.constructor.l?.forEach(t=>t(this))}addController(t){(this._$EO??=new Set).add(t),void 0!==this.renderRoot&&this.isConnected&&t.hostConnected?.()}removeController(t){this._$EO?.delete(t)}_$E_(){const t=new Map,e=this.constructor.elementProperties;for(const i of e.keys())this.hasOwnProperty(i)&&(t.set(i,this[i]),delete this[i]);t.size>0&&(this._$Ep=t)}createRenderRoot(){const t=this.shadowRoot??this.attachShadow(this.constructor.shadowRootOptions);return((t,s)=>{if(i)t.adoptedStyleSheets=s.map(t=>t instanceof CSSStyleSheet?t:t.styleSheet);else for(const i of s){const s=document.createElement("style"),n=e.litNonce;void 0!==n&&s.setAttribute("nonce",n),s.textContent=i.cssText,t.appendChild(s)}})(t,this.constructor.elementStyles),t}connectedCallback(){this.renderRoot??=this.createRenderRoot(),this.enableUpdating(!0),this._$EO?.forEach(t=>t.hostConnected?.())}enableUpdating(t){}disconnectedCallback(){this._$EO?.forEach(t=>t.hostDisconnected?.())}attributeChangedCallback(t,e,i){this._$AK(t,i)}_$ET(t,e){const i=this.constructor.elementProperties.get(t),s=this.constructor._$Eu(t,i);if(void 0!==s&&!0===i.reflect){const n=(void 0!==i.converter?.toAttribute?i.converter:f).toAttribute(e,i.type);this._$Em=t,null==n?this.removeAttribute(s):this.setAttribute(s,n),this._$Em=null}}_$AK(t,e){const i=this.constructor,s=i._$Eh.get(t);if(void 0!==s&&this._$Em!==s){const t=i.getPropertyOptions(s),n="function"==typeof t.converter?{fromAttribute:t.converter}:void 0!==t.converter?.fromAttribute?t.converter:f;this._$Em=s;const r=n.fromAttribute(e,t.type);this[s]=r??this._$Ej?.get(s)??r,this._$Em=null}}requestUpdate(t,e,i,s=!1,n){if(void 0!==t){const r=this.constructor;if(!1===s&&(n=this[t]),i??=r.getPropertyOptions(t),!((i.hasChanged??y)(n,e)||i.useDefault&&i.reflect&&n===this._$Ej?.get(t)&&!this.hasAttribute(r._$Eu(t,i))))return;this.C(t,e,i)}!1===this.isUpdatePending&&(this._$ES=this._$EP())}C(t,e,{useDefault:i,reflect:s,wrapped:n},r){i&&!(this._$Ej??=new Map).has(t)&&(this._$Ej.set(t,r??e??this[t]),!0!==n||void 0!==r)||(this._$AL.has(t)||(this.hasUpdated||i||(e=void 0),this._$AL.set(t,e)),!0===s&&this._$Em!==t&&(this._$Eq??=new Set).add(t))}async _$EP(){this.isUpdatePending=!0;try{await this._$ES}catch(t){Promise.reject(t)}const t=this.scheduleUpdate();return null!=t&&await t,!this.isUpdatePending}scheduleUpdate(){return this.performUpdate()}performUpdate(){if(!this.isUpdatePending)return;if(!this.hasUpdated){if(this.renderRoot??=this.createRenderRoot(),this._$Ep){for(const[t,e]of this._$Ep)this[t]=e;this._$Ep=void 0}const t=this.constructor.elementProperties;if(t.size>0)for(const[e,i]of t){const{wrapped:t}=i,s=this[e];!0!==t||this._$AL.has(e)||void 0===s||this.C(e,void 0,i,s)}}let t=!1;const e=this._$AL;try{t=this.shouldUpdate(e),t?(this.willUpdate(e),this._$EO?.forEach(t=>t.hostUpdate?.()),this.update(e)):this._$EM()}catch(e){throw t=!1,this._$EM(),e}t&&this._$AE(e)}willUpdate(t){}_$AE(t){this._$EO?.forEach(t=>t.hostUpdated?.()),this.hasUpdated||(this.hasUpdated=!0,this.firstUpdated(t)),this.updated(t)}_$EM(){this._$AL=new Map,this.isUpdatePending=!1}get updateComplete(){return this.getUpdateComplete()}getUpdateComplete(){return this._$ES}shouldUpdate(t){return!0}update(t){this._$Eq&&=this._$Eq.forEach(t=>this._$ET(t,this[t])),this._$EM()}updated(t){}firstUpdated(t){}};$.elementStyles=[],$.shadowRootOptions={mode:"open"},$[_("elementProperties")]=new Map,$[_("finalized")]=new Map,b?.({ReactiveElement:$}),(g.reactiveElementVersions??=[]).push("2.1.2");const x=globalThis,k=t=>t,A=x.trustedTypes,S=A?A.createPolicy("lit-html",{createHTML:t=>t}):void 0,C="$lit$",E=`lit$${Math.random().toFixed(9).slice(2)}$`,N="?"+E,O=`<${N}>`,P=document,M=()=>P.createComment(""),z=t=>null===t||"object"!=typeof t&&"function"!=typeof t,H=Array.isArray,U="[ \t\n\f\r]",T=/<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g,j=/-->/g,R=/>/g,L=RegExp(`>|${U}(?:([^\\s"'>=/]+)(${U}*=${U}*(?:[^ \t\n\f\r"'\`<>=]|("|')|))|$)`,"g"),I=/'/g,D=/"/g,W=/^(?:script|style|textarea|title)$/i,q=(t=>(e,...i)=>({_$litType$:t,strings:e,values:i}))(1),B=Symbol.for("lit-noChange"),F=Symbol.for("lit-nothing"),V=new WeakMap,J=P.createTreeWalker(P,129);function K(t,e){if(!H(t)||!t.hasOwnProperty("raw"))throw Error("invalid template strings array");return void 0!==S?S.createHTML(e):e}const Y=(t,e)=>{const i=t.length-1,s=[];let n,r=2===e?"<svg>":3===e?"<math>":"",a=T;for(let e=0;e<i;e++){const i=t[e];let o,c,l=-1,d=0;for(;d<i.length&&(a.lastIndex=d,c=a.exec(i),null!==c);)d=a.lastIndex,a===T?"!--"===c[1]?a=j:void 0!==c[1]?a=R:void 0!==c[2]?(W.test(c[2])&&(n=RegExp("</"+c[2],"g")),a=L):void 0!==c[3]&&(a=L):a===L?">"===c[0]?(a=n??T,l=-1):void 0===c[1]?l=-2:(l=a.lastIndex-c[2].length,o=c[1],a=void 0===c[3]?L:'"'===c[3]?D:I):a===D||a===I?a=L:a===j||a===R?a=T:(a=L,n=void 0);const h=a===L&&t[e+1].startsWith("/>")?" ":"";r+=a===T?i+O:l>=0?(s.push(o),i.slice(0,l)+C+i.slice(l)+E+h):i+E+(-2===l?e:h)}return[K(t,r+(t[i]||"<?>")+(2===e?"</svg>":3===e?"</math>":"")),s]};class Z{constructor({strings:t,_$litType$:e},i){let s;this.parts=[];let n=0,r=0;const a=t.length-1,o=this.parts,[c,l]=Y(t,e);if(this.el=Z.createElement(c,i),J.currentNode=this.el.content,2===e||3===e){const t=this.el.content.firstChild;t.replaceWith(...t.childNodes)}for(;null!==(s=J.nextNode())&&o.length<a;){if(1===s.nodeType){if(s.hasAttributes())for(const t of s.getAttributeNames())if(t.endsWith(C)){const e=l[r++],i=s.getAttribute(t).split(E),a=/([.?@])?(.*)/.exec(e);o.push({type:1,index:n,name:a[2],strings:i,ctor:"."===a[1]?et:"?"===a[1]?it:"@"===a[1]?st:tt}),s.removeAttribute(t)}else t.startsWith(E)&&(o.push({type:6,index:n}),s.removeAttribute(t));if(W.test(s.tagName)){const t=s.textContent.split(E),e=t.length-1;if(e>0){s.textContent=A?A.emptyScript:"";for(let i=0;i<e;i++)s.append(t[i],M()),J.nextNode(),o.push({type:2,index:++n});s.append(t[e],M())}}}else if(8===s.nodeType)if(s.data===N)o.push({type:2,index:n});else{let t=-1;for(;-1!==(t=s.data.indexOf(E,t+1));)o.push({type:7,index:n}),t+=E.length-1}n++}}static createElement(t,e){const i=P.createElement("template");return i.innerHTML=t,i}}function G(t,e,i=t,s){if(e===B)return e;let n=void 0!==s?i._$Co?.[s]:i._$Cl;const r=z(e)?void 0:e._$litDirective$;return n?.constructor!==r&&(n?._$AO?.(!1),void 0===r?n=void 0:(n=new r(t),n._$AT(t,i,s)),void 0!==s?(i._$Co??=[])[s]=n:i._$Cl=n),void 0!==n&&(e=G(t,n._$AS(t,e.values),n,s)),e}class Q{constructor(t,e){this._$AV=[],this._$AN=void 0,this._$AD=t,this._$AM=e}get parentNode(){return this._$AM.parentNode}get _$AU(){return this._$AM._$AU}u(t){const{el:{content:e},parts:i}=this._$AD,s=(t?.creationScope??P).importNode(e,!0);J.currentNode=s;let n=J.nextNode(),r=0,a=0,o=i[0];for(;void 0!==o;){if(r===o.index){let e;2===o.type?e=new X(n,n.nextSibling,this,t):1===o.type?e=new o.ctor(n,o.name,o.strings,this,t):6===o.type&&(e=new nt(n,this,t)),this._$AV.push(e),o=i[++a]}r!==o?.index&&(n=J.nextNode(),r++)}return J.currentNode=P,s}p(t){let e=0;for(const i of this._$AV)void 0!==i&&(void 0!==i.strings?(i._$AI(t,i,e),e+=i.strings.length-2):i._$AI(t[e])),e++}}class X{get _$AU(){return this._$AM?._$AU??this._$Cv}constructor(t,e,i,s){this.type=2,this._$AH=F,this._$AN=void 0,this._$AA=t,this._$AB=e,this._$AM=i,this.options=s,this._$Cv=s?.isConnected??!0}get parentNode(){let t=this._$AA.parentNode;const e=this._$AM;return void 0!==e&&11===t?.nodeType&&(t=e.parentNode),t}get startNode(){return this._$AA}get endNode(){return this._$AB}_$AI(t,e=this){t=G(this,t,e),z(t)?t===F||null==t||""===t?(this._$AH!==F&&this._$AR(),this._$AH=F):t!==this._$AH&&t!==B&&this._(t):void 0!==t._$litType$?this.$(t):void 0!==t.nodeType?this.T(t):(t=>H(t)||"function"==typeof t?.[Symbol.iterator])(t)?this.k(t):this._(t)}O(t){return this._$AA.parentNode.insertBefore(t,this._$AB)}T(t){this._$AH!==t&&(this._$AR(),this._$AH=this.O(t))}_(t){this._$AH!==F&&z(this._$AH)?this._$AA.nextSibling.data=t:this.T(P.createTextNode(t)),this._$AH=t}$(t){const{values:e,_$litType$:i}=t,s="number"==typeof i?this._$AC(t):(void 0===i.el&&(i.el=Z.createElement(K(i.h,i.h[0]),this.options)),i);if(this._$AH?._$AD===s)this._$AH.p(e);else{const t=new Q(s,this),i=t.u(this.options);t.p(e),this.T(i),this._$AH=t}}_$AC(t){let e=V.get(t.strings);return void 0===e&&V.set(t.strings,e=new Z(t)),e}k(t){H(this._$AH)||(this._$AH=[],this._$AR());const e=this._$AH;let i,s=0;for(const n of t)s===e.length?e.push(i=new X(this.O(M()),this.O(M()),this,this.options)):i=e[s],i._$AI(n),s++;s<e.length&&(this._$AR(i&&i._$AB.nextSibling,s),e.length=s)}_$AR(t=this._$AA.nextSibling,e){for(this._$AP?.(!1,!0,e);t!==this._$AB;){const e=k(t).nextSibling;k(t).remove(),t=e}}setConnected(t){void 0===this._$AM&&(this._$Cv=t,this._$AP?.(t))}}class tt{get tagName(){return this.element.tagName}get _$AU(){return this._$AM._$AU}constructor(t,e,i,s,n){this.type=1,this._$AH=F,this._$AN=void 0,this.element=t,this.name=e,this._$AM=s,this.options=n,i.length>2||""!==i[0]||""!==i[1]?(this._$AH=Array(i.length-1).fill(new String),this.strings=i):this._$AH=F}_$AI(t,e=this,i,s){const n=this.strings;let r=!1;if(void 0===n)t=G(this,t,e,0),r=!z(t)||t!==this._$AH&&t!==B,r&&(this._$AH=t);else{const s=t;let a,o;for(t=n[0],a=0;a<n.length-1;a++)o=G(this,s[i+a],e,a),o===B&&(o=this._$AH[a]),r||=!z(o)||o!==this._$AH[a],o===F?t=F:t!==F&&(t+=(o??"")+n[a+1]),this._$AH[a]=o}r&&!s&&this.j(t)}j(t){t===F?this.element.removeAttribute(this.name):this.element.setAttribute(this.name,t??"")}}class et extends tt{constructor(){super(...arguments),this.type=3}j(t){this.element[this.name]=t===F?void 0:t}}class it extends tt{constructor(){super(...arguments),this.type=4}j(t){this.element.toggleAttribute(this.name,!!t&&t!==F)}}class st extends tt{constructor(t,e,i,s,n){super(t,e,i,s,n),this.type=5}_$AI(t,e=this){if((t=G(this,t,e,0)??F)===B)return;const i=this._$AH,s=t===F&&i!==F||t.capture!==i.capture||t.once!==i.once||t.passive!==i.passive,n=t!==F&&(i===F||s);s&&this.element.removeEventListener(this.name,this,i),n&&this.element.addEventListener(this.name,this,t),this._$AH=t}handleEvent(t){"function"==typeof this._$AH?this._$AH.call(this.options?.host??this.element,t):this._$AH.handleEvent(t)}}class nt{constructor(t,e,i){this.element=t,this.type=6,this._$AN=void 0,this._$AM=e,this.options=i}get _$AU(){return this._$AM._$AU}_$AI(t){G(this,t)}}const rt=x.litHtmlPolyfillSupport;rt?.(Z,X),(x.litHtmlVersions??=[]).push("3.3.3");const at=globalThis;class ot extends ${constructor(){super(...arguments),this.renderOptions={host:this},this._$Do=void 0}createRenderRoot(){const t=super.createRenderRoot();return this.renderOptions.renderBefore??=t.firstChild,t}update(t){const e=this.render();this.hasUpdated||(this.renderOptions.isConnected=this.isConnected),super.update(t),this._$Do=((t,e,i)=>{const s=i?.renderBefore??e;let n=s._$litPart$;if(void 0===n){const t=i?.renderBefore??null;s._$litPart$=n=new X(e.insertBefore(M(),t),t,void 0,i??{})}return n._$AI(t),n})(e,this.renderRoot,this.renderOptions)}connectedCallback(){super.connectedCallback(),this._$Do?.setConnected(!0)}disconnectedCallback(){super.disconnectedCallback(),this._$Do?.setConnected(!1)}render(){return B}}ot._$litElement$=!0,ot.finalized=!0,at.litElementHydrateSupport?.({LitElement:ot});const ct=at.litElementPolyfillSupport;ct?.({LitElement:ot}),(at.litElementVersions??=[]).push("4.2.2");const lt=t=>(e,i)=>{void 0!==i?i.addInitializer(()=>{customElements.define(t,e)}):customElements.define(t,e)},dt={attribute:!0,type:String,converter:f,reflect:!1,hasChanged:y},ht=(t=dt,e,i)=>{const{kind:s,metadata:n}=i;let r=globalThis.litPropertyMetadata.get(n);if(void 0===r&&globalThis.litPropertyMetadata.set(n,r=new Map),"setter"===s&&((t=Object.create(t)).wrapped=!0),r.set(i.name,t),"accessor"===s){const{name:s}=i;return{set(i){const n=e.get.call(this);e.set.call(this,i),this.requestUpdate(s,n,t,!0,i)},init(e){return void 0!==e&&this.C(s,void 0,t,e),e}}}if("setter"===s){const{name:s}=i;return function(i){const n=this[s];e.call(this,i),this.requestUpdate(s,n,t,!0,i)}}throw Error("Unsupported decorator location: "+s)};function pt(t){return(e,i)=>"object"==typeof i?ht(t,e,i):((t,e,i)=>{const s=e.hasOwnProperty(i);return e.constructor.createProperty(i,t),s?Object.getOwnPropertyDescriptor(e,i):void 0})(t,e,i)}function ut(t){return pt({...t,state:!0,attribute:!1})}function gt(t,e,i){for(const s of e){const[e]=s.split(".",1),n=s.split(".")[1]??"",r=i.states[s]?.attributes.device_class;"sensor"===e?"temperature"===r?t.temperature=s:"humidity"===r?t.humidity=s:"illuminance"===r&&(t.light=s):"binary_sensor"===e?"motion"===r||n.endsWith("_motion")||n.endsWith("_cloud_motion")?t.motion=s:("sound"===r||n.endsWith("_sound")||n.endsWith("_cloud_sound"))&&(t.sound=s):"switch"===e&&n.endsWith("_camera_power")?t.power=s:"light"===e&&n.endsWith("_night_light")&&!n.includes("sl_")?t.night_light=s:"media_player"===e&&n.endsWith("_sound_machine")&&(t.sound_machine=s)}}function mt(t,e){if(!e)return!1;const i=t.entities[e];return!i?.disabled_by&&e in t.states}const vt=a`
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
`;let bt=class extends ot{setConfig(t){this._config={...t}}_entityChanged(t,e){const i=e.detail.value||void 0;this._config&&i!==this._config[t]&&this._updateConfig({[t]:i})}_toggleChanged(t,e){const i=e.target.checked;this._updateConfig({[t]:i})}_updateConfig(t){const e={...this._config,...t};for(const[i,s]of Object.entries(t))void 0===s&&delete e[i];this._config=e,this.dispatchEvent(new CustomEvent("config-changed",{bubbles:!0,composed:!0,detail:{config:e}}))}render(){return this.hass&&this._config?q`
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
    `:F}};bt.styles=a`
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
  `,t([pt({attribute:!1})],bt.prototype,"hass",void 0),t([ut()],bt.prototype,"_config",void 0),bt=t([lt("nanit-card-editor")],bt);let _t=class extends ot{constructor(){super(...arguments),this._streamLoaded=!1,this._showNetwork=!1}static getConfigElement(){return document.createElement("nanit-card-editor")}static getStubConfig(t){const e=Object.keys(t.states).find(e=>e.startsWith("camera.")&&"nanit"===t.entities[e]?.platform);return{type:"custom:nanit-card",camera_entity_id:e||""}}setConfig(t){if(!t)throw new Error("Invalid configuration");this._config=t}getCardSize(){return 5}_entities(){return this._config?.camera_entity_id&&this.hass?function(t,e){const i={};i.camera=e;const s=t.entities[e];if(s?.device_id){const e=s.device_id,n=[],r=[];for(const i of Object.keys(t.entities))t.entities[i].device_id===e&&("diagnostic"===t.entities[i].entity_category?r.push(i):n.push(i));gt(i,n,t),function(t,e,i){for(const s of e){const[e]=s.split(".",1),n=s.split(".")[1]??"",r=i.states[s]?.attributes.device_class;"sensor"===e&&("signal_strength"===r?t.wifi_signal=s:"frequency"===r?t.wifi_frequency=s:n.endsWith("_wifi_ssid")&&(t.wifi_ssid=s))}}(i,r,t)}else{const s=e.split(".")[1]??"";gt(i,Object.keys(t.states).filter(t=>t!==e&&t.split(".")[1]?.startsWith(s.split("_camera")[0]||s)),t)}return i}(this.hass,this._config.camera_entity_id):{}}_isCameraOn(t){return!t.power||"on"===this.hass.states[t.power]?.state}_fireMoreInfo(t){this.dispatchEvent(new CustomEvent("hass-more-info",{bubbles:!0,composed:!0,detail:{entityId:t}}))}_toggleService(t,e,i){this.hass.callService(t,e,{entity_id:i})}disconnectedCallback(){super.disconnectedCallback(),this._clearStreamCheck()}updated(t){if(super.updated(t),!this._streamLoaded&&!this._streamCheckTimer){this.renderRoot.querySelector("ha-camera-stream")&&this._scheduleStreamCheck()}}_scheduleStreamCheck(){this._streamCheckTimer=setTimeout(()=>{this._streamCheckTimer=void 0;const t=this.renderRoot.querySelector("ha-camera-stream");if(!t)return;const e=t.shadowRoot;e?.querySelector("video, img, canvas")?this._streamLoaded=!0:this._scheduleStreamCheck()},500)}_clearStreamCheck(){this._streamCheckTimer&&(clearTimeout(this._streamCheckTimer),this._streamCheckTimer=void 0)}_onStreamLoad(){this._streamLoaded=!0,this._clearStreamCheck()}render(){if(!this.hass||!this._config)return q`<ha-card><div class="header"><span class="device-name">Nanit</span></div></ha-card>`;const t=this._entities(),e=this._isCameraOn(t);!e&&this._streamLoaded&&(this._streamLoaded=!1,this._clearStreamCheck());const i=t.camera?function(t,e){const i=t.states[e];return i?(i.attributes.friendly_name??"Nanit").replace(/ Camera$/i,""):"Nanit"}(this.hass,t.camera):"Nanit";return q`
      <ha-card>
        ${this._renderHeader(i,t,e)}
        <div class="card-content ${e?"":"collapsed"}">
          ${e?this._renderStream(t):F}
          ${e?this._renderControls(t):F}
        </div>
      </ha-card>
    `}_renderHeader(t,e,i){const s=!this._config.hide_connectivity_status&&(mt(this.hass,e.wifi_ssid)||mt(this.hass,e.wifi_signal)||mt(this.hass,e.wifi_frequency)),n=!this._config.hide_baby_name,r=e.power&&!this._config.hide_power_button;return n||!i||s||r?q`
      <div class="header">
        ${n?q`
              <div class="device-badge">
                <ha-icon icon="mdi:baby-face-outline"></ha-icon>
                <span class="device-name">${t}</span>
              </div>
            `:F}
        ${i?F:q`<span class="camera-off-label">Camera Off</span>`}
        <div class="header-actions">
          ${s?q`
                <button
                  class="wifi-btn"
                  @click=${()=>{this._showNetwork=!this._showNetwork}}
                >
                  <ha-icon icon="mdi:wifi"></ha-icon>
                </button>
              `:F}
          ${r?q`
                <button
                  class="power-btn ${i?"":"off"}"
                  @click=${()=>this._toggleService("switch","toggle",e.power)}
                >
                  <ha-icon icon="mdi:power"></ha-icon>
                </button>
              `:F}
        </div>
      </div>
      ${this._showNetwork?this._renderNetworkPopup(e):F}
    `:q``}_renderNetworkPopup(t){const e=t.wifi_ssid?this.hass.states[t.wifi_ssid]?.state:void 0,i=t.wifi_signal?this.hass.states[t.wifi_signal]?.state:void 0,s=t.wifi_signal?this.hass.states[t.wifi_signal]?.attributes.unit_of_measurement??"dBm":"dBm",n=t.wifi_frequency?this.hass.states[t.wifi_frequency]?.state:void 0,r=t.wifi_frequency?this.hass.states[t.wifi_frequency]?.attributes.unit_of_measurement??"MHz":"MHz",a=i?parseInt(i,10):-100;let o="Weak",c="#e74c3c";return a>=-50?(o="Excellent",c="#2ecc71"):a>=-60?(o="Good",c="var(--nanit-teal)"):a>=-70&&(o="Fair",c="var(--nanit-amber)"),q`
      <div class="network-backdrop" @click=${()=>{this._showNetwork=!1}}></div>
      <div class="network-popup">
        <div class="network-header">
          <ha-icon icon="mdi:wifi"></ha-icon>
          <span>Network</span>
        </div>
        ${e?q`
              <div class="network-row">
                <ha-icon icon="mdi:router-wireless"></ha-icon>
                <div class="network-detail">
                  <span class="network-label">WiFi Name</span>
                  <span class="network-value">${e}</span>
                </div>
              </div>
            `:F}
        ${i?q`
              <div class="network-row">
                <ha-icon icon="mdi:signal" style="color: ${c}"></ha-icon>
                <div class="network-detail">
                  <span class="network-label">Signal Strength</span>
                  <span class="network-value">${i} ${s} · <span style="color: ${c}">${o}</span></span>
                </div>
              </div>
            `:F}
        ${n?q`
              <div class="network-row">
                <ha-icon icon="mdi:frequency"></ha-icon>
                <div class="network-detail">
                  <span class="network-label">Frequency</span>
                  <span class="network-value">${n} ${r}</span>
                </div>
              </div>
            `:F}
      </div>
    `}_renderStream(t){const e=t.camera?this.hass.states[t.camera]:void 0;return q`
      <div class="stream-wrap">
        ${e?q`
              <div
                class="stream-click"
                @click=${()=>t.camera&&this._fireMoreInfo(t.camera)}
              >
                <ha-camera-stream
                  muted
                  .hass=${this.hass}
                  .stateObj=${e}
                  @load=${this._onStreamLoad}
                ></ha-camera-stream>
              </div>
              <div class="stream-loader ${this._streamLoaded?"hidden":""}">
                <div class="loader-content">
                  <ha-icon icon="mdi:camera"></ha-icon>
                  <div class="loader-spinner"></div>
                </div>
              </div>
            `:q`
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
    `}_renderSensorOverlays(t){const e=[],i=this._config.temperature_entity_id||t.temperature,s=this._config.humidity_entity_id||t.humidity;if(mt(this.hass,i)){const t=parseFloat(this.hass.states[i].state),s=isNaN(t)?this.hass.states[i].state:t.toFixed(1),n=this.hass.states[i].attributes.unit_of_measurement??"";e.push(q`
        <div class="pill pill-temp" @click=${()=>this._fireMoreInfo(i)}>
          <ha-icon icon="mdi:thermometer"></ha-icon>
          <span>${s}${n}</span>
        </div>
      `)}if(mt(this.hass,s)){const t=parseFloat(this.hass.states[s].state),i=isNaN(t)?this.hass.states[s].state:t.toFixed(1),n=this.hass.states[s].attributes.unit_of_measurement??"%";e.push(q`
        <div class="pill pill-humid" @click=${()=>this._fireMoreInfo(s)}>
          <ha-icon icon="mdi:water-percent"></ha-icon>
          <span>${i}${n}</span>
        </div>
      `)}return 0===e.length?q``:q`<div class="overlay-top">${e}</div>`}_renderDetectionOverlays(t){const e=mt(this.hass,t.motion),i=mt(this.hass,t.sound);if(!e&&!i)return q``;const s=e&&"on"===this.hass.states[t.motion].state,n=i&&"on"===this.hass.states[t.sound].state;return q`
      <div class="overlay-bottom">
        ${e?q`
              <div
                class="pill ${s?"active motion-active":""}"
                @click=${()=>this._fireMoreInfo(t.motion)}
              >
                <ha-icon icon="mdi:motion-sensor"></ha-icon>
                <span>${s?"Motion":"Clear"}</span>
              </div>
            `:q`<div></div>`}
        ${i?q`
              <div
                class="pill ${n?"active sound-active":""}"
                @click=${()=>this._fireMoreInfo(t.sound)}
              >
                <ha-icon icon="mdi:ear-hearing"></ha-icon>
                <span>${n?"Sound":"Quiet"}</span>
              </div>
            `:F}
      </div>
    `}_renderControls(t){const e=!this._config.hide_night_light&&mt(this.hass,t.night_light),i=!this._config.hide_sound_machine&&mt(this.hass,t.sound_machine);return e||i?q`
      <div class="controls">
        ${e?this._renderNightLight(t.night_light):F}
        ${i?this._renderSoundMachine(t.sound_machine):F}
      </div>
    `:q``}_renderNightLight(t){const e=this.hass.states[t],i="on"===e?.state,s=e?.attributes.brightness??0,n=Math.round(s/255*100);return q`
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
            <div class="nanit-slider" style="--slider-pct: ${n}%">
              <input
                type="range"
                min="0"
                max="100"
                .value=${String(n)}
                @input=${t=>{const e=t.target.closest(".nanit-slider");e&&e.style.setProperty("--slider-pct",`${t.target.value}%`)}}
                @change=${e=>{const i=Number(e.target.value);0===i?this.hass.callService("light","turn_off",{entity_id:t}):this.hass.callService("light","turn_on",{entity_id:t,brightness:Math.round(i/100*255)})}}
              />
            </div>
          </div>
        </div>
      </div>
    `}_renderSoundMachine(t){const e=this.hass.states[t],i="playing"===e?.state,s=e?.attributes.source??"",n=e?.attributes.source_list??[],r=e?.attributes.volume_level??0,a=Math.round(100*r);return q`
      <div class="control-section control-section-sound">
        <div class="section-header">
          <span class="control-label">Sound Machine</span>
          ${n.length>0?q`
                <div class="source-list">
                  ${n.map(e=>q`
                      <button
                        class="source-icon ${e===s?"active":""}"
                        title=${this._formatSourceName(e)}
                        @click=${()=>this.hass.callService("media_player","select_source",{entity_id:t,source:e})}
                      >
                        <ha-icon icon=${this._sourceIcon(e)}></ha-icon>
                      </button>
                    `)}
                </div>
              `:F}
        </div>
        <div class="control-row">
          <button
            class="icon-btn ${i?"active":""}"
            @click=${()=>this._toggleService("media_player",i?"media_stop":"media_play",t)}
          >
            <ha-icon icon="mdi:${i?"stop":"play"}"></ha-icon>
          </button>
          ${i?q`<span class="track-name">${this._formatSourceName(s)}</span>`:F}
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
          </div>
        </div>
      </div>
    `}_sourceIcon(t){return{white_noise:"mdi:sine-wave",birds:"mdi:bird",waves:"mdi:waves",wind:"mdi:weather-windy",rain:"mdi:weather-rainy",water_stream:"mdi:water",fan:"mdi:fan",heartbeat:"mdi:heart-pulse",dryer:"mdi:tumble-dryer",vacuum:"mdi:robot-vacuum"}[t.replace(/\.wav$/i,"").toLowerCase()]??"mdi:music-note"}_formatSourceName(t){return t.replace(/\.wav$/i,"").replace(/_/g," ")}};_t.styles=vt,t([pt({attribute:!1})],_t.prototype,"hass",void 0),t([ut()],_t.prototype,"_config",void 0),t([ut()],_t.prototype,"_streamLoaded",void 0),t([ut()],_t.prototype,"_showNetwork",void 0),_t=t([lt("nanit-card")],_t),window.customCards=window.customCards||[],window.customCards.push({type:"nanit-card",name:"Nanit Camera",description:"Camera stream with controls for Nanit baby cameras",preview:!0});export{_t as NanitCard};
