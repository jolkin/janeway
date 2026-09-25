(define (problem put-out-fires)
  (:domain drone-firefighting)

  (:objects
    drone1 drone2 - drone
    between - location
    home house1 house2 house3 - house
    lake - water-source
  )

  (:init
    (drone-at drone1 home)
    (tank-empty drone1)
    (drone-at drone2 home)
    (tank-empty drone2)
    (fire house1)
    (fire house2)
    (fire house3)
  )

  (:goal (and
    (extinguished house1)
    (extinguished house2)
    (extinguished house3)
  ))
)
