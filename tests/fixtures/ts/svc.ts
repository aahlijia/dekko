export interface Item {
  id: number;
}

export function fetchItem(id: number, eager?: boolean): Item {
  return { id };
}

export class Service {
  private items: Item[] = [];

  add(item: Item): void {
    this.items.push(item);
  }

  load(id: number): Item {
    const item = fetchItem(id);
    this.add(item);
    return item;
  }
}
