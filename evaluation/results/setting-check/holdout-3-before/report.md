# Setting check evaluation

- Runs per case: 3
- Recall: 0.889 (stable in every run: 0.667)
- Trap rate (reported something it must not): 0.0
- Precision (findings that are expected or acceptable): 1.0
- Unlisted findings per run: 0.0
- Run-to-run agreement (Jaccard): 0.87
- Median seconds per check: 18.0
- Tokens: input 24667, cached 0, output 4145

| Case | Expected (hits/runs) | Finder saw | Traps hit | Unlisted | Agreement |
| --- | --- | --- | --- | --- | --- |
| h3-first-ship | — | — | 0 | 0 | 1.0 |
| h3-dead-sister | sister-back 2/3 | sister-back 3/3 | 0 | 0 | 0.33 |
| h3-alias-age | young-mayor 3/3 | young-mayor 3/3 | 0 | 0 | 1.0 |
| h3-clean-growth | — | — | 0 | 0 | 1.0 |
| h3-recent-chapter-rule | flying 3/3 | flying 3/3 | 0 | 0 | 1.0 |

## Findings

### h3-first-ship · 첫 배(앞 회차의 사실을 건너는 모순)
- run 1: 0 found, 0 unverified, 0 kept, 5.5s
- run 2: 0 found, 0 unverified, 0 kept, 3.4s
- run 3: 0 found, 0 unverified, 0 kept, 4.3s

### h3-dead-sister · 죽은 언니와 회상 뒤의 현재
- run 1: 1 found, 0 unverified, 1 kept, 10.6s
  - [expect:sister-back] (HIGH) “오늘 아침, 세린은 공방 문을 열고 들어와 오나의 작업대 옆에 앉았다.” ← c1: “오나의 언니 세린은 지난겨울 병으로 세상을 떠났다.”
    1화 「장례」에서는 세린이 지난겨울 병으로 세상을 떠났다고 정해 두었어요. 그런데 2화에서 오늘 아침 세린이 공방에 들어와 오나의 작업대 옆에 앉았다고 서술해요. 별도의 설명이 없으므로 세린이 죽은 상태이면서 평범하게 공방에 들어와 앉았다는 두 내용은 함께 성립하지 않아요. 세린이 돌아왔거나 유령으로 나타났다는 경위도 원고에 나오지 않아요.
- run 2: 1 found, 0 unverified, 0 kept, 10.7s; judge dropped: sister-back→CHARACTER_PERSPECTIVE
- run 3: 1 found, 0 unverified, 1 kept, 12.3s
  - [expect:sister-back] (HIGH) “오늘 아침, 세린은 공방 문을 열고 들어와 오나의 작업대 옆에 앉았다.” ← c1: “오나의 언니 세린은 지난겨울 병으로 세상을 떠났다.”
    1화 「장례」에서는 오나의 언니 세린이 지난겨울 병으로 세상을 떠났다고 밝혀요. 그런데 2화에서 세린은 오늘 아침 공방에 들어와 오나의 작업대 옆에 앉아요. 세린이 살아 돌아왔거나 유령으로 나타났다는 설명이 없어서, 두 장면은 현재 사실로 함께 성립하지 않아요.

### h3-alias-age · 직함으로 부른 인물의 나이
- run 1: 1 found, 0 unverified, 1 kept, 16.2s
  - [expect:young-mayor] (HIGH) “갓 스무 살이 된 촌장이 축제의 첫 춤을 추었다.” ← mayor: “바닷가 마을의 촌장. 일흔 살 노인이다.”
    ‘하윤’ 설정은 촌장이 일흔 살 노인이라고 정해 두었어요. 그런데 2화에서는 촌장이 갓 스무 살이 된 인물로 나와요. 같은 시점의 촌장이 일흔 살이면서 갓 스무 살일 수는 없고, 나이가 달라졌거나 다른 촌장이라는 설명도 없어요.
- run 2: 1 found, 0 unverified, 1 kept, 18.0s
  - [expect:young-mayor] (HIGH) “갓 스무 살이 된 촌장이 축제의 첫 춤을 추었다.” ← mayor: “바닷가 마을의 촌장. 일흔 살 노인이다.”
    ‘하윤’ 설정 문서는 촌장을 일흔 살 노인으로 정해 두었어요. 그런데 2화에서는 같은 촌장이 갓 스무 살이 된 것으로 묘사돼요. 두 나이는 그 시점에 함께 참일 수 없고, 나이가 달라진 까닭이나 다른 인물이라는 설명도 나오지 않아요.
- run 3: 1 found, 0 unverified, 1 kept, 18.2s
  - [expect:young-mayor] (HIGH) “갓 스무 살이 된 촌장이 축제의 첫 춤을 추었다.” ← mayor: “바닷가 마을의 촌장. 일흔 살 노인이다.”
    ‘하윤’ 설정은 촌장이 일흔 살 노인이라고 정해 두었어요. 그런데 2화 축제 장면에서는 촌장이 갓 스무 살이 된 인물로 나와요. 같은 시점의 촌장이 일흔 살이면서 갓 스무 살일 수는 없고, 나이가 달라진 까닭이나 다른 촌장이라는 설명도 나오지 않아요.

### h3-clean-growth · 이유가 나온 성장
- run 1: 0 found, 0 unverified, 0 kept, 18.1s
- run 2: 0 found, 0 unverified, 0 kept, 20.6s
- run 3: 0 found, 0 unverified, 0 kept, 20.5s

### h3-recent-chapter-rule · 앞 회차가 정한 규칙
- run 1: 1 found, 0 unverified, 1 kept, 24.0s
  - [expect:flying] (HIGH) “첫 수업이 끝나자 리오는 학원 안뜰 위를 날아 기숙사 창문으로 들어갔다.” ← c1: “입학식에서 학장은 말했다. 학원 안에서는 누구도 날 수 없다.”
    1화 「입학」에서 학장은 탑의 결계가 비행 마법을 막아 학원 안에서는 아무도 날 수 없다고 했어요. 그런데 2화에서 첫 수업이 끝난 뒤 리오는 학원 안뜰 위를 날아 기숙사 창문으로 들어가요. 같은 학원 안에서 리오가 실제로 날았으므로 두 내용은 함께 성립할 수 없고, 결계를 우회했거나 다른 이유로 날 수 있었다는 설명도 나오지 않아요.
- run 2: 1 found, 0 unverified, 1 kept, 27.0s
  - [expect:flying] (HIGH) “첫 수업이 끝나자 리오는 학원 안뜰 위를 날아 기숙사 창문으로 들어갔다.” ← c1: “학원 안에서는 누구도 날 수 없다.”
    1화 「입학」에서는 탑의 결계가 비행 마법을 막아 학원 안에서는 누구도 날 수 없다고 정해 두었어요. 그런데 2화에서 첫 수업이 끝난 뒤 리오는 학원 안뜰 위를 날아 기숙사 창문으로 들어가요. 결계가 사라지거나 리오가 비행할 수 있었던 까닭은 원고에 나오지 않아, 두 내용은 이 시점에 함께 성립할 수 없어요.
- run 3: 1 found, 0 unverified, 1 kept, 26.4s
  - [expect:flying] (HIGH) “첫 수업이 끝나자 리오는 학원 안뜰 위를 날아 기숙사 창문으로 들어갔다.” ← c1: “학원 안에서는 누구도 날 수 없다.”
    1화 「입학」에서 학장은 탑의 결계가 비행 마법을 막아 학원 안에서는 누구도 날 수 없다고 밝혀요. 그런데 2화에서 첫 수업이 끝난 뒤 리오는 학원 안뜰 위를 날아 기숙사 창문으로 들어가요. 두 내용은 같은 학원 안에서 함께 성립할 수 없고, 결계가 풀리거나 리오가 예외가 되는 이유도 원고에 나오지 않아요.

