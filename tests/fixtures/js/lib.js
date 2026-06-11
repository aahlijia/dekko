export function greet(name) {
  return `hi ${name}`;
}

export class Greeter {
  constructor(prefix = "Hello") {
    this.prefix = prefix;
  }

  greetAll(...names) {
    return names.map((n) => greet(n));
  }
}
