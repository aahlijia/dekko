(ns shapes.core
  (:require [clojure.string :as str]))

(defprotocol Shape
  (area [this]))

(defn- square [x]
  (* x x))

(defrecord Circle [radius]
  Shape
  (area [this] (* Math/PI (square radius))))

(defn describe
  "Describe a shape."
  [shape]
  (let [value (area shape)]
    (str/join " " ["area" value])))

(println (describe (->Circle 2)))
