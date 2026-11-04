import "./dep.js";
import * as up from "../up.js";
export * from "./reexport.js";
export * as named from "./named.js";
import*as m from"./minified.js";
import("./dynamic.js");
const img = new Image();
img.src = new URL("./asset.png", import.meta.url);
