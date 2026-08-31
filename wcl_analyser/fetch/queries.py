"""GraphQL documents.

Kept as literals in one place so the fetch modules stay readable and so a
document can be hashed into a cache key when its shape changes.
"""

from __future__ import annotations

import hashlib

REPORT_META = """
query($code: String!, $killType: KillType!) {
  reportData {
    report(code: $code) {
      code
      title
      startTime
      endTime
      segments
      zone { id name }
      owner { name }
      guild { name }
      region { slug }
      fights(killType: $killType) {
        id
        encounterID
        name
        difficulty
        size
        kill
        startTime
        endTime
        fightPercentage
        bossPercentage
        lastPhase
        friendlyPlayers
      }
      playerDetails(
        startTime: 0
        endTime: 99999999999
        killType: $killType
        includeCombatantInfo: false
      )
      masterData(translate: true) {
        abilities { gameID name icon type }
        actors { id gameID name type subType icon server petOwner }
      }
    }
  }
}
"""

PLAYER_DETAILS = """
query($code: String!, $fightID: Int!) {
  reportData {
    report(code: $code) {
      playerDetails(fightIDs: [$fightID], includeCombatantInfo: false)
    }
  }
}
"""

EVENTS = """
query(
  $code: String!
  $start: Float!
  $end: Float!
  $fightID: Int!
  $dataType: EventDataType!
  $hostility: HostilityType!
  $sourceID: Int
  $targetID: Int
  $limit: Int!
  $includeResources: Boolean!
) {
  reportData {
    report(code: $code) {
      events(
        startTime: $start
        endTime: $end
        fightIDs: [$fightID]
        dataType: $dataType
        hostilityType: $hostility
        sourceID: $sourceID
        targetID: $targetID
        limit: $limit
        includeResources: $includeResources
        useAbilityIDs: true
        useActorIDs: true
      ) {
        data
        nextPageTimestamp
      }
    }
  }
}
"""

TABLE = """
query(
  $code: String!
  $start: Float!
  $end: Float!
  $fightID: Int!
  $dataType: TableDataType!
  $hostility: HostilityType!
  $sourceID: Int
  $targetID: Int
) {
  reportData {
    report(code: $code) {
      table(
        startTime: $start
        endTime: $end
        fightIDs: [$fightID]
        dataType: $dataType
        hostilityType: $hostility
        sourceID: $sourceID
        targetID: $targetID
      )
    }
  }
}
"""

GRAPH = """
query(
  $code: String!
  $start: Float!
  $end: Float!
  $fightID: Int!
  $dataType: GraphDataType!
  $hostility: HostilityType!
  $sourceID: Int
  $targetID: Int
) {
  reportData {
    report(code: $code) {
      graph(
        startTime: $start
        endTime: $end
        fightIDs: [$fightID]
        dataType: $dataType
        hostilityType: $hostility
        sourceID: $sourceID
        targetID: $targetID
      )
    }
  }
}
"""


def document_hash(document: str) -> str:
    """Short stable hash of a query document, recorded in each cache blob."""
    return hashlib.sha256(document.encode("utf-8")).hexdigest()[:12]
