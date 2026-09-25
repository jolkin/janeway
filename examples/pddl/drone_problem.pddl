(define (problem put-out-fires)
  (:domain drone-firefighting)

  (:objects
    drone1 - drone
    between - location
    home house1 house2 - house
    lake - water-source
  )

  (:init
    (drone-at drone1 home)
    (tank-empty drone1)
    (fire house1)
    (fire house2)
  )

  (:goal (and
    (extinguished house1)
    (extinguished house2)
  ))
)
