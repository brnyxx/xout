# 8축 x 3장면 판정표 (scene coverage table)

catalog_version: v2
status: 픽스처 v2 저작 이전에 확정. 이 표가 fixtures/scenes.json 저작 형태를 결정한다.
(v1의 전역/국소 이원 구조는 은퇴했다 - response_language, verbosity 축은 v2 카탈로그에서
제거됐고, 구버전 이벤트의 해당 긋기는 재생 시 관용 처리된다.)

## 판정 기준

- **모든 축은 슬롯화된다**: 축의 값 차이가 트랜스크립트의 특정 지점 한 곳에서만
  관측된다. 같은 skeleton을 공유하고 그 축의 슬롯 조각만 3변형으로 교체하면
  대비가 성립한다. 배경 슬롯은 채굴 최빈값(카탈로그 튜플 index 0)으로 고정된다.
- **맥락 클래스**: 장면은 routine(일상 작업) 또는 irreversible(되돌리기 어려운 작업)
  중 하나에 속한다. 두 맥락에서 모두 측정되는 축(교차 축)은 맥락별 생존값이 갈릴 때
  조건부 규칙으로 컴파일된다. 조건은 추정이 아니라 긋기 증거에서만 나온다.

## 장면 구성

| 장면 | 맥락 | 판별 축 (5) |
|---|---|---|
| scn-bugfix (페이지네이션 수정) | routine | autonomy, scope_adherence, test_discipline, comment_doc, error_behavior |
| scn-feature (CSV 내보내기 추가) | routine | scope_adherence, test_discipline, dependency_policy, verification, commit_style |
| scn-risky (스키마 마이그레이션) | irreversible | autonomy, error_behavior, verification, dependency_policy, commit_style |

## 축 커버리지

| 축 | routine | irreversible | 교차(조건부 가능) |
|---|---|---|---|
| autonomy | scn-bugfix | scn-risky | O |
| error_behavior | scn-bugfix | scn-risky | O |
| verification | scn-feature | scn-risky | O |
| dependency_policy | scn-feature | scn-risky | O |
| commit_style | scn-feature | scn-risky | O |
| scope_adherence | scn-bugfix, scn-feature | - | X (routine 교차 검증) |
| test_discipline | scn-bugfix, scn-feature | - | X (routine 교차 검증) |
| comment_doc | scn-bugfix | - | X (단일 장면 - 맥락 불변 스타일 축) |

## 스케줄링 계약

- 페어 목록은 라운드 교차 순서다: 라운드 r = 각 (장면, 축)의 r번째 값 조합.
  세션은 자연히 장면1 -> 장면2 -> 장면3 순서로 흐른다.
- 판별력 판정은 **페어가 속한 맥락의 생존값 기준**이다. routine에서 지워진 값이
  irreversible 페어의 판별력을 죽이지 않는다 (맥락 간 오염 금지).
- 세션 유효성은 판별 증거가 남은 축 수로 판정한다(봉인 하한 5축). 다중 장면
  설계에서 완전 판별(생존 1값)은 맥락 간 값 분화에 달려 있어 세션 품질의
  지표가 아니다.
