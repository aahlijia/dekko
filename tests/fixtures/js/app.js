import { greet, Greeter } from "./lib.js";

const main = () => {
  const g = new Greeter("yo");
  g.greetAll("a", "b");
  return greet("x");
};

main();
