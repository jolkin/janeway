(define (domain drone-firefighting)
  (:requirements :typing :durative-actions)

  (:types
    drone location - Object
    house water-source - location
  )

  (:predicates
    (drone-at ?d - drone ?l - location)
    (has-water ?d - drone)
    (tank-empty ?d - drone)
    (fire ?l - location)
    (extinguished ?l - location)
  )

  ;uncontrollable
  (:durative-action fly
    :parameters (?d - drone ?from - location ?to - location)
    :duration (and (<= ?duration 10) (>= ?duration 5))
    :condition (and
      (at start (drone-at ?d ?from))
    )
    :effect (and
      (at start (not (drone-at ?d ?from)))
      (at end (drone-at ?d ?to))
    )
  )

  (:durative-action scoop
    :parameters (?d - drone ?l - water-source)
    :duration (= ?duration 1)
    :condition (and
      (over all (drone-at ?d ?l))
      (at start (tank-empty ?d))
    )
    :effect (and
      (at start (not (tank-empty ?d)))
      (at end (has-water ?d))
    )
  )

  (:durative-action deliver
    :parameters (?d - drone ?l - house)
    :duration (= ?duration 1) 
    :condition (and
      (over all (drone-at ?d ?l))
      (at start (has-water ?d))
      (at start (fire ?l))
    )
    :effect (and
      (at start (not (fire ?l)))
      (at start (not (has-water ?d)))
      (at end (tank-empty ?d))
      (at end (extinguished ?l))
    )
  )
)
