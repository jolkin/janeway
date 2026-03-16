;;; Quicklisp dependencies needed to build the kirk-v2 binary.
;;; This is a subset of enterprise/dependencies.lisp that excludes packages
;;; with heavy system dependencies (e.g. mcclim requires X11/CLX) that are not
;;; needed for the planning server.

(map nil
     (lambda (pkg)
       (handler-case (ql:quickload pkg :silent t)
         (error (e)
           (format t "WARNING: failed to load ~A: ~A (continuing)~%" pkg e))))
     '(:40ants-doc
       :adopt
       :alexandria
       :anaphora
       :atomics
       :bordeaux-threads
       :cffi
       :cl-ansi-term
       :cl-ansi-text
       :cl-fad
       :cl-heap
       :cl-json
       :cl-ppcre
       :cl-semver
       :cl-singleton-mixin
       :cl-store
       :cl+ssl
       :clingon
       :concrete-syntax-tree
       :deploy
       :drakma
       :easy-routes
       :eclector
       :esrap
       :fiveam
       :float-features
       :genhash
       :hunchentoot
       :iterate
       :inferior-shell
       :jsown
       :lisp-namespace
       :local-time
       :log4cl
       :macroexpand-dammit
       :metap
       :mockingbird
       :named-readtables
       :parachute
       :parse-number
       :pileup
       :queues
       :reader-interception
       :s-xml
       :safe-queue
       :salza2
       :shasht
       :split-sequence
       :str
       :trivial-cltl2
       :trivial-features
       :trivial-garbage
       :trivial-indent
       :trivial-raw-io
       :trivial-utf-8
       :type-i
       :uuid))
