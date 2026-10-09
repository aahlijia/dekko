import { Widget as WD } from "./svc";
export class Hub {
  private config: Config;
  readonly name = "hub";
  count = 0;
  private items: Thing[] = [];
  private w = new WD();
  handler = () => this.config.load();
  constructor(private readonly mcpHub: McpHub, readonly port?: number, plain: string) {
    this.extra = new Extra();
    this.late = makeLate();
  }
  go() { const local = new Config(); this.config.load(); }
}
export interface Ctx { host: SessionHost; run(): void; opt?: Opts; cb: { inner: Foo } }
export class Dup {
  x: Foo;
  constructor() { this.x = new Bar(); }
}
