<!-- GENERATED from the library's own function registry. Do not edit by hand. -->

# Functions the library declines in a DEFAULT or MATERIALIZED expression

This is the complete, generated list of every ClickHouse function that a column `DEFAULT`, `EPHEMERAL` or `MATERIALIZED` expression cannot use through this library, and the reason for each (chtypes#479). The library does not guess: it asks the vendored ClickHouse registry about every function and applies one fixed admission rule, in a fixed order. This page is that rule run over the registry of each supported line, so it cannot say anything the library does not do.

Generated from production build `20261006.170903`, which carries the ClickHouse patches in the [Lines](#lines) table. It is refreshed whenever the library is rebuilt, not when an SDK is released.

A declined function is a decline, not a rejection: the schema is valid ClickHouse, and the library will not evaluate it. A function that is not in the tables is either admitted or not a function of the registry at all (an aggregate, a window function, a user-defined function or an unknown name, each of which has its own answer).

## What is admitted

Everything deterministic that is not listed below, plus:

- **Clock reads** (`now`, `now64`, `today`, `yesterday`): evaluated once for the batch, at one instant, and the same instant for every row.
- **Random generators** (22 names on the admitted list): admitted only in a column's own `DEFAULT` or `EPHEMERAL` expression, where the vendored function draws a fresh value per row. They are declined in a `MATERIALIZED` expression, a `CHECK`, a `TTL`, a filter, a partition key and a `Values` field, because each of those is answered against a value the server draws itself. The names are in [Generators](#generators).

## How the rule decides

In this order, the first match wins:

1. [Needs a server](#needs-a-server)
2. [Access refused (446)](#access-refused-446)
3. [Server identity](#server-identity)
4. [Stateful](#stateful)
5. [Volatile](#volatile)
6. [Newer verdict](#newer-verdict)

## Lines

| Line | Exact patch | Registry functions | Needs a server | Access refused (446) | Server identity | Stateful | Volatile | Newer verdict | Declined in total |
| ---- | ----------- | ------------------ | -------------- | -------------------- | --------------- | -------- | -------- | ------------- | ----------------- |
| 26.3 | 26.3.38.2   | 1442               | 35             | 1                    | 18              | 5        | 99       | 16            | 174               |
| 26.7 | 26.7.19.5   | 1503               | 45             | 1                    | 18              | 5        | 119      | 3             | 191               |
| 26.8 | 26.8.15.10  | 1529               | 36             | 1                    | 18              | 21       | 123      | 3             | 202               |
| 26.9 | 26.9.8.3    | 1541               | 36             | 1                    | 30              | 21       | 114      | 0             | 202               |

The registry is read from the library's own function registry at each line's pinned patch. Every build re-reads its own registry and fails when it disagrees with the committed capture.

## Needs a server

The function's resolver throws when it is built outside a running ClickHouse server (the error code is ClickHouse's own and is shown per function). The library declines it: the value does not exist off a server.

| Function                                  | Declined on         | Detail    |
| ----------------------------------------- | ------------------- | --------- |
| `AIClassify`                              | 26.7                | error 344 |
| `AIExtract`                               | 26.7                | error 344 |
| `AIGenerate`                              | 26.7                | error 344 |
| `AITranslate`                             | 26.7                | error 344 |
| `aiClassify`                              | 26.7                | error 344 |
| `aiEmbed`                                 | 26.7                | error 344 |
| `aiExtract`                               | 26.7                | error 344 |
| `aiGenerate`                              | 26.7                | error 344 |
| `aiTranslate`                             | 26.7                | error 344 |
| `fuzzQuery`                               | all supported lines | error 344 |
| `neighbor`                                | all supported lines | error 721 |
| `regionHierarchy`                         | all supported lines | error 156 |
| `regionIn`                                | all supported lines | error 156 |
| `regionToArea`                            | all supported lines | error 156 |
| `regionToCity`                            | all supported lines | error 156 |
| `regionToContinent`                       | all supported lines | error 156 |
| `regionToCountry`                         | all supported lines | error 156 |
| `regionToDistrict`                        | all supported lines | error 156 |
| `regionToName`                            | all supported lines | error 156 |
| `regionToPopulation`                      | all supported lines | error 156 |
| `regionToTopContinent`                    | all supported lines | error 156 |
| `runningAccumulate`                       | all supported lines | error 721 |
| `runningDifference`                       | all supported lines | error 721 |
| `runningDifferenceStartingWithFirstValue` | all supported lines | error 721 |
| `serverUUID`                              | all supported lines | error 49  |
| `showCertificate`                         | all supported lines | error 344 |
| `timeSeriesCopyTag`                       | all supported lines | error 393 |
| `timeSeriesCopyTags`                      | all supported lines | error 393 |
| `timeSeriesExtractTag`                    | all supported lines | error 393 |
| `timeSeriesGroupToSamplingKey`            | 26.7, 26.8, 26.9    | error 393 |
| `timeSeriesGroupToTags`                   | all supported lines | error 393 |
| `timeSeriesIdToGroup`                     | all supported lines | error 393 |
| `timeSeriesIdToTags`                      | all supported lines | error 393 |
| `timeSeriesIdToTagsGroup`                 | all supported lines | error 393 |
| `timeSeriesJoinTags`                      | all supported lines | error 393 |
| `timeSeriesRemoveAllTagsExcept`           | all supported lines | error 393 |
| `timeSeriesRemoveTag`                     | all supported lines | error 393 |
| `timeSeriesRemoveTags`                    | all supported lines | error 393 |
| `timeSeriesReplaceTag`                    | all supported lines | error 393 |
| `timeSeriesStoreTags`                     | all supported lines | error 393 |
| `timeSeriesTagsGroupToTags`               | all supported lines | error 393 |
| `timeSeriesTagsToGroup`                   | all supported lines | error 393 |
| `timeSeriesThrowDuplicateSeriesIf`        | all supported lines | error 393 |
| `transactionLatestSnapshot`               | all supported lines | error 48  |
| `transactionOldestSnapshot`               | all supported lines | error 48  |

## Access refused (446)

The function is refused by ClickHouse's own access check (error 446, FUNCTION_NOT_ALLOWED) at default settings. The library reports that verdict as the server gives it (a rejection, not a decline) unless the declared CREATE profile enables the function.

| Function              | Declined on         | Detail    |
| --------------------- | ------------------- | --------- |
| `getClientHTTPHeader` | all supported lines | error 446 |

## Server identity

ClickHouse itself marks the function server-constant: its value is a property of the server (host name, version, uptime, time zone, ports, shard), not of the client. Answering it here would store the gateway's answer.

| Function                   | Declined on         |
| -------------------------- | ------------------- |
| `FQDN`                     | 26.9                |
| `__getScalar`              | all supported lines |
| `addressToLine`            | 26.9                |
| `addressToLineWithInlines` | 26.9                |
| `addressToSymbol`          | 26.9                |
| `buildId`                  | all supported lines |
| `displayName`              | all supported lines |
| `filesystemAvailable`      | 26.9                |
| `filesystemCapacity`       | 26.9                |
| `filesystemUnreserved`     | 26.9                |
| `fullHostName`             | 26.9                |
| `getMacro`                 | all supported lines |
| `getOSKernelVersion`       | all supported lines |
| `getServerPort`            | 26.9                |
| `hostName`                 | all supported lines |
| `hostname`                 | all supported lines |
| `queryID`                  | 26.9                |
| `query_id`                 | 26.9                |
| `revision`                 | all supported lines |
| `serverTimeZone`           | all supported lines |
| `serverTimezone`           | all supported lines |
| `shardCount`               | all supported lines |
| `shardNum`                 | all supported lines |
| `tcpPort`                  | all supported lines |
| `timeZone`                 | all supported lines |
| `timezone`                 | all supported lines |
| `transactionID`            | 26.9                |
| `uptime`                   | all supported lines |
| `version`                  | all supported lines |
| `zookeeperSessionUptime`   | all supported lines |

## Stateful

ClickHouse marks the function stateful: its value depends on insertion order, a block or row number, or an external service. A client cannot reproduce it.

| Function               | Declined on         |
| ---------------------- | ------------------- |
| `AIClassify`           | 26.8, 26.9          |
| `AIEmbed`              | 26.8, 26.9          |
| `AIExtract`            | 26.8, 26.9          |
| `AIFilter`             | 26.8, 26.9          |
| `AIGenerate`           | 26.8, 26.9          |
| `AIRedact`             | 26.8, 26.9          |
| `AISimilarity`         | 26.8, 26.9          |
| `AITranslate`          | 26.8, 26.9          |
| `aiClassify`           | 26.8, 26.9          |
| `aiEmbed`              | 26.8, 26.9          |
| `aiExtract`            | 26.8, 26.9          |
| `aiFilter`             | 26.8, 26.9          |
| `aiGenerate`           | 26.8, 26.9          |
| `aiRedact`             | 26.8, 26.9          |
| `aiSimilarity`         | 26.8, 26.9          |
| `aiTranslate`          | 26.8, 26.9          |
| `blockNumber`          | all supported lines |
| `generateSerialID`     | all supported lines |
| `rowNumberInAllBlocks` | all supported lines |
| `rowNumberInBlock`     | all supported lines |
| `runningConcurrency`   | all supported lines |

## Volatile

ClickHouse marks the function non-deterministic and it is neither one of the four clock reads nor on the admitted generator list. It depends on the session, the block, a file, a machine id or a sequence, so a client cannot reproduce the value the server would store.

| Function                           | Declined on         |
| ---------------------------------- | ------------------- |
| `DATABASE`                         | all supported lines |
| `FQDN`                             | 26.3, 26.7, 26.8    |
| `SCHEMA`                           | all supported lines |
| `UTCTimestamp`                     | all supported lines |
| `UTC_timestamp`                    | all supported lines |
| `__applyFilter`                    | 26.7, 26.8, 26.9    |
| `arrayJoin`                        | all supported lines |
| `arrayPartialReverseSort`          | 26.7, 26.8, 26.9    |
| `arrayPartialShuffle`              | 26.7, 26.8, 26.9    |
| `arrayPartialSort`                 | 26.7, 26.8, 26.9    |
| `arrayRandomSample`                | 26.7, 26.8, 26.9    |
| `arrayShuffle`                     | 26.7, 26.8, 26.9    |
| `assignCentroid`                   | 26.9                |
| `authUser`                         | all supported lines |
| `authenticatedUser`                | all supported lines |
| `blockSerializedSize`              | 26.7, 26.8, 26.9    |
| `blockSize`                        | all supported lines |
| `catboostEvaluate`                 | 26.3, 26.7, 26.8    |
| `connectionId`                     | all supported lines |
| `connection_id`                    | all supported lines |
| `curdate`                          | all supported lines |
| `currentDatabase`                  | all supported lines |
| `currentHandler`                   | 26.8, 26.9          |
| `currentProfiles`                  | all supported lines |
| `currentQueryID`                   | all supported lines |
| `currentRequestURL`                | 26.8, 26.9          |
| `currentRoles`                     | all supported lines |
| `currentSchemas`                   | all supported lines |
| `currentUser`                      | all supported lines |
| `current_database`                 | all supported lines |
| `current_date`                     | all supported lines |
| `current_query_id`                 | all supported lines |
| `current_schemas`                  | all supported lines |
| `current_timestamp`                | all supported lines |
| `current_user`                     | all supported lines |
| `defaultProfiles`                  | all supported lines |
| `defaultRoles`                     | all supported lines |
| `dictGet`                          | all supported lines |
| `dictGetAll`                       | all supported lines |
| `dictGetChildren`                  | all supported lines |
| `dictGetDate`                      | all supported lines |
| `dictGetDateOrDefault`             | all supported lines |
| `dictGetDateTime`                  | all supported lines |
| `dictGetDateTimeOrDefault`         | all supported lines |
| `dictGetDescendants`               | all supported lines |
| `dictGetFloat32`                   | all supported lines |
| `dictGetFloat32OrDefault`          | all supported lines |
| `dictGetFloat64`                   | all supported lines |
| `dictGetFloat64OrDefault`          | all supported lines |
| `dictGetHierarchy`                 | all supported lines |
| `dictGetIPv4`                      | all supported lines |
| `dictGetIPv4OrDefault`             | all supported lines |
| `dictGetIPv6`                      | all supported lines |
| `dictGetIPv6OrDefault`             | all supported lines |
| `dictGetInt16`                     | all supported lines |
| `dictGetInt16OrDefault`            | all supported lines |
| `dictGetInt32`                     | all supported lines |
| `dictGetInt32OrDefault`            | all supported lines |
| `dictGetInt64`                     | all supported lines |
| `dictGetInt64OrDefault`            | all supported lines |
| `dictGetInt8`                      | all supported lines |
| `dictGetInt8OrDefault`             | all supported lines |
| `dictGetKeys`                      | all supported lines |
| `dictGetOrDefault`                 | all supported lines |
| `dictGetOrNull`                    | all supported lines |
| `dictGetRoot`                      | 26.7, 26.8, 26.9    |
| `dictGetString`                    | all supported lines |
| `dictGetStringOrDefault`           | all supported lines |
| `dictGetUInt16`                    | all supported lines |
| `dictGetUInt16OrDefault`           | all supported lines |
| `dictGetUInt32`                    | all supported lines |
| `dictGetUInt32OrDefault`           | all supported lines |
| `dictGetUInt64`                    | all supported lines |
| `dictGetUInt64OrDefault`           | all supported lines |
| `dictGetUInt8`                     | all supported lines |
| `dictGetUInt8OrDefault`            | all supported lines |
| `dictGetUUID`                      | all supported lines |
| `dictGetUUIDOrDefault`             | all supported lines |
| `dictHas`                          | all supported lines |
| `dictIsIn`                         | all supported lines |
| `dumpColumnStructure`              | 26.7, 26.8, 26.9    |
| `enabledProfiles`                  | all supported lines |
| `enabledRoles`                     | all supported lines |
| `file`                             | all supported lines |
| `filesystemAvailable`              | 26.3, 26.7, 26.8    |
| `filesystemCapacity`               | 26.3, 26.7, 26.8    |
| `filesystemUnreserved`             | 26.3, 26.7, 26.8    |
| `financialNetPresentValueExtended` | all supported lines |
| `fullHostName`                     | 26.3, 26.7, 26.8    |
| `generateRandomStructure`          | 26.3                |
| `generateSnowflakeID`              | all supported lines |
| `getMergeTreeSetting`              | all supported lines |
| `getServerPort`                    | 26.3, 26.7, 26.8    |
| `getServerSetting`                 | all supported lines |
| `getSetting`                       | all supported lines |
| `getSettingOrDefault`              | all supported lines |
| `hasColumnInTable`                 | all supported lines |
| `initialQueryID`                   | all supported lines |
| `initialQueryStartTime`            | all supported lines |
| `initial_query_id`                 | all supported lines |
| `initial_query_start_time`         | all supported lines |
| `isConstant`                       | 26.7, 26.8, 26.9    |
| `joinGet`                          | all supported lines |
| `joinGetOrNull`                    | all supported lines |
| `localtime`                        | 26.7, 26.8, 26.9    |
| `localtimestamp`                   | 26.7, 26.8, 26.9    |
| `lowCardinalityIndices`            | 26.7, 26.8, 26.9    |
| `lowCardinalityKeys`               | 26.7, 26.8, 26.9    |
| `naiveBayesClassifier`             | 26.7, 26.8, 26.9    |
| `naiveBayesClassifierWithAllProbs` | 26.7, 26.8, 26.9    |
| `naiveBayesClassifierWithProb`     | 26.7, 26.8, 26.9    |
| `nowInBlock`                       | all supported lines |
| `nowInBlock64`                     | all supported lines |
| `obfuscateQuery`                   | 26.7, 26.8, 26.9    |
| `pgGetUserById`                    | 26.8, 26.9          |
| `pg_get_userbyid`                  | 26.8, 26.9          |
| `queryID`                          | 26.3, 26.7, 26.8    |
| `query_id`                         | 26.3, 26.7, 26.8    |
| `rand32`                           | all supported lines |
| `randConstant`                     | all supported lines |
| `session_user`                     | 26.7, 26.8, 26.9    |
| `toColumnTypeName`                 | 26.7, 26.8, 26.9    |
| `transactionID`                    | 26.3, 26.7, 26.8    |
| `unnest`                           | 26.7, 26.8, 26.9    |
| `user`                             | all supported lines |

## Newer verdict

This line's own flag says deterministic, but the newest served line that has the function calls it non-deterministic (a later upstream correction of the flag). Upstream corrected the flag later, so the value this build would compute is not one the stored row is bound to match.

| Function                   | Declined on      | Detail                  |
| -------------------------- | ---------------- | ----------------------- |
| `__applyFilter`            | 26.3             | newest served line 26.9 |
| `addressToLine`            | 26.3, 26.7, 26.8 | newest served line 26.9 |
| `addressToLineWithInlines` | 26.3, 26.7, 26.8 | newest served line 26.9 |
| `addressToSymbol`          | 26.3, 26.7, 26.8 | newest served line 26.9 |
| `arrayPartialReverseSort`  | 26.3             | newest served line 26.9 |
| `arrayPartialShuffle`      | 26.3             | newest served line 26.9 |
| `arrayPartialSort`         | 26.3             | newest served line 26.9 |
| `arrayRandomSample`        | 26.3             | newest served line 26.9 |
| `arrayShuffle`             | 26.3             | newest served line 26.9 |
| `blockSerializedSize`      | 26.3             | newest served line 26.9 |
| `dumpColumnStructure`      | 26.3             | newest served line 26.9 |
| `isConstant`               | 26.3             | newest served line 26.9 |
| `lowCardinalityIndices`    | 26.3             | newest served line 26.9 |
| `lowCardinalityKeys`       | 26.3             | newest served line 26.9 |
| `naiveBayesClassifier`     | 26.3             | newest served line 26.9 |
| `toColumnTypeName`         | 26.3             | newest served line 26.9 |

## Generators

Admitted in a column's own `DEFAULT` or `EPHEMERAL` expression, declined everywhere else (including `MATERIALIZED`). A name is listed on the lines whose registry has it and reads as a fresh per-row draw.

| Function               | Admitted in DEFAULT on |
| ---------------------- | ---------------------- |
| `dateTimeToUUIDv7`     | all supported lines    |
| `fuzzBits`             | all supported lines    |
| `generateUUIDv4`       | all supported lines    |
| `generateUUIDv7`       | all supported lines    |
| `rand`                 | all supported lines    |
| `rand64`               | all supported lines    |
| `randBernoulli`        | all supported lines    |
| `randBinomial`         | all supported lines    |
| `randCanonical`        | all supported lines    |
| `randChiSquared`       | all supported lines    |
| `randExponential`      | all supported lines    |
| `randFisherF`          | all supported lines    |
| `randLogNormal`        | all supported lines    |
| `randNegativeBinomial` | all supported lines    |
| `randNormal`           | all supported lines    |
| `randPoisson`          | all supported lines    |
| `randStudentT`         | all supported lines    |
| `randUniform`          | all supported lines    |
| `randomFixedString`    | all supported lines    |
| `randomPrintableASCII` | all supported lines    |
| `randomString`         | all supported lines    |
| `randomStringUTF8`     | all supported lines    |

## What this list does not cover

These are decided from the expression, not from one function, and stay prose rules:

- A `DEFAULT` that reads a column whose own `DEFAULT` is declined is declined with it.
- A function's arguments can decline a call the list admits (a constant that does not parse, a type the function does not accept).
- A setting-dependent answer, such as the 446 above under a profile that enables the function.
