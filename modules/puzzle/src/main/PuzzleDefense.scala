package lila.puzzle

import play.api.libs.json.*

/* Defense puzzles: the opponent's last move creates a threat,
 * and exactly one legal move avoids losing material or getting checkmated.
 *
 * They're found in played games by bin/defense-puzzles/generate.py,
 * which also stores the engine refutation of every wrong move,
 * so the client can show what each mistake loses without running an engine.
 * For now the set is small and shipped as a classpath resource. */
object PuzzleDefense:

  private val resource = "/defensePuzzles.json"

  lazy val json: JsArray =
    val stream = Option(getClass.getResourceAsStream(resource)).getOrElse:
      sys.error(s"Missing defense puzzles resource $resource")
    try Json.parse(stream).as[JsArray]
    finally stream.close()
