// Tipi minimi del runtime Cloudflare usati qui (nessun pacchetto @cloudflare/workers-types installato).
declare module "*.html" {
  const text: string;
  export default text;
}
declare module "*.csv" {
  const text: string;
  export default text;
}
declare module "cloudflare:workers" {
  export abstract class DurableObject {
    protected ctx: DurableObjectState;
    protected env: unknown;
    constructor(ctx: DurableObjectState, env: unknown);
    fetch?(request: Request): Promise<Response>;
    alarm?(): Promise<void>;
  }
}
interface DurableObjectState {
  readonly storage: unknown;
  blockConcurrencyWhile<T>(fn: () => Promise<T>): Promise<T>;
}
interface DurableObjectStub {
  fetch(request: Request): Promise<Response>;
}
interface DurableObjectNamespace {
  idFromName(name: string): unknown;
  get(id: unknown): DurableObjectStub;
}
declare module "*.js" {
  const text: string;
  export default text;
}
declare module "*.webmanifest" {
  const text: string;
  export default text;
}
declare module "*.png" {
  const data: ArrayBuffer;
  export default data;
}
