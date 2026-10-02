;;; shapes.el --- Areas of shapes  -*- lexical-binding: t -*-

(cl-defstruct shapes-circle radius)

(defun shapes-square (x)
  "Square X."
  (* x x))

(defun shapes-area (c)
  "Area of circle C."
  (* float-pi (shapes-square (shapes-circle-radius c))))

(defmacro shapes-twice (form)
  `(* 2 ,form))

(cl-defun shapes-describe (c &key (prefix "area"))
  (let ((value (shapes-area c)))
    (format "%s %s" prefix value)))

(message (shapes-describe (make-shapes-circle :radius 2)))
