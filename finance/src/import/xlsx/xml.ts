/**
 * Parser XML minimo per le parti di un XLSX (nessun DOMParser nei Workers). Supporta elementi, attributi,
 * testo, entita' predefinite e numeriche, CDATA; ignora commenti, istruzioni di elaborazione e DOCTYPE.
 * Rifiuta DTD con entita' (nessuna espansione: niente "billion laughs") e XML mal annidato.
 * I nomi degli elementi perdono il prefisso di namespace (es. `x:row` -> `row`); gli attributi lo conservano.
 */

export interface XmlElement {
  readonly name: string;
  readonly attrs: Readonly<Record<string, string>>;
  readonly children: (XmlElement | string)[];
}

export class XmlError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "XmlError";
  }
}

const ENTITIES: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };

export function decodeEntities(text: string): string {
  return text.replace(/&(#x[0-9a-fA-F]+|#\d+|[a-zA-Z]+);/g, (whole, ent: string) => {
    if (ent.startsWith("#x")) return String.fromCodePoint(Number.parseInt(ent.slice(2), 16));
    if (ent.startsWith("#")) return String.fromCodePoint(Number.parseInt(ent.slice(1), 10));
    const v = ENTITIES[ent];
    if (v === undefined) throw new XmlError(`entita' sconosciuta: ${whole}`);
    return v;
  });
}

const localName = (n: string) => n.slice(n.indexOf(":") + 1);

function parseAttrs(src: string): Record<string, string> {
  const attrs: Record<string, string> = {};
  const rx = /([A-Za-z_][\w:.-]*)\s*=\s*("([^"]*)"|'([^']*)')/g;
  for (let m = rx.exec(src); m !== null; m = rx.exec(src)) {
    attrs[m[1] ?? ""] = decodeEntities(m[3] ?? m[4] ?? "");
  }
  if (src.replace(rx, "").trim() !== "")
    throw new XmlError(`attributi non validi: ${src.trim().slice(0, 40)}`);
  return attrs;
}

export function parseXml(text: string): XmlElement {
  const root: XmlElement = { name: "#document", attrs: {}, children: [] };
  const stack: XmlElement[] = [root];
  let i = 0;
  const top = () => stack[stack.length - 1] ?? root;
  while (i < text.length) {
    const lt = text.indexOf("<", i);
    if (lt === -1) {
      if (text.slice(i).trim() !== "" && stack.length > 1) top().children.push(decodeEntities(text.slice(i)));
      break;
    }
    if (lt > i) {
      const t = text.slice(i, lt);
      if (stack.length > 1) top().children.push(decodeEntities(t));
    }
    if (text.startsWith("<?", lt)) {
      const end = text.indexOf("?>", lt);
      if (end === -1) throw new XmlError("istruzione non chiusa");
      i = end + 2;
    } else if (text.startsWith("<!--", lt)) {
      const end = text.indexOf("-->", lt);
      if (end === -1) throw new XmlError("commento non chiuso");
      i = end + 3;
    } else if (text.startsWith("<![CDATA[", lt)) {
      const end = text.indexOf("]]>", lt);
      if (end === -1) throw new XmlError("CDATA non chiuso");
      top().children.push(text.slice(lt + 9, end));
      i = end + 3;
    } else if (text.startsWith("<!", lt)) {
      const end = text.indexOf(">", lt);
      if (end === -1 || text.slice(lt, end).includes("[")) throw new XmlError("DTD non supportata");
      i = end + 1;
    } else {
      const end = text.indexOf(">", lt);
      if (end === -1) throw new XmlError("tag non chiuso");
      const inner = text.slice(lt + 1, end);
      if (inner.startsWith("/")) {
        const name = localName(inner.slice(1).trim());
        const open = stack.pop();
        if (open === undefined || open === root || open.name !== name) {
          throw new XmlError(`chiusura inattesa: </${name}>`);
        }
      } else {
        const selfClosing = inner.endsWith("/");
        const body = selfClosing ? inner.slice(0, -1) : inner;
        const m = /^([A-Za-z_][\w:.-]*)([\s\S]*)$/.exec(body);
        if (m === null) throw new XmlError(`tag non valido: <${inner.slice(0, 40)}>`);
        const el: XmlElement = { name: localName(m[1] ?? ""), attrs: parseAttrs(m[2] ?? ""), children: [] };
        top().children.push(el);
        if (!selfClosing) stack.push(el);
      }
      i = end + 1;
    }
  }
  if (stack.length !== 1) throw new XmlError(`elemento non chiuso: <${top().name}>`);
  const docEl = root.children.find((c): c is XmlElement => typeof c !== "string");
  if (docEl === undefined) throw new XmlError("documento XML vuoto");
  return docEl;
}

export function childElements(el: XmlElement, name?: string): XmlElement[] {
  return el.children.filter(
    (c): c is XmlElement => typeof c !== "string" && (name === undefined || c.name === name),
  );
}

export function firstChild(el: XmlElement, name: string): XmlElement | undefined {
  return childElements(el, name)[0];
}

/** Testo concatenato dei discendenti, escludendo gli elementi indicati (es. `rPh`, la pronuncia fonetica). */
export function textContent(el: XmlElement, exclude: readonly string[] = []): string {
  return el.children
    .map((c) => (typeof c === "string" ? c : exclude.includes(c.name) ? "" : textContent(c, exclude)))
    .join("");
}
