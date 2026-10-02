(defclass circle ()
  ((radius :initarg :radius :accessor radius)))

(defstruct point x y)

(defun square (x)
  (* x x))

(defmethod area ((c circle))
  (* pi (square (radius c))))

(defmacro twice (form)
  `(* 2 ,form))

(defun describe-shape (c &optional (stream t))
  (let ((value (area c)))
    (format stream "~a" value)))

(describe-shape (make-instance 'circle :radius 2))
