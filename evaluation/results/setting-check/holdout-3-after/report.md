# Setting check evaluation

- Runs per case: 3
- Recall: 1.0 (stable in every run: 1.0)
- Trap rate (reported something it must not): 0.0
- Precision (findings that are expected or acceptable): 1.0
- Unlisted findings per run: 0.0
- Run-to-run agreement (Jaccard): 1.0
- Median seconds per check: 20.6
- Tokens: input 40726, cached 17303, output 6348

| Case | Expected (hits/runs) | Finder saw | Traps hit | Unlisted | Agreement |
| --- | --- | --- | --- | --- | --- |
| h3-first-ship | — | — | 0 | 0 | 1.0 |
| h3-dead-sister | sister-back 3/3 | sister-back 3/3 | 0 | 0 | 1.0 |
| h3-alias-age | young-mayor 3/3 | young-mayor 3/3 | 0 | 0 | 1.0 |
| h3-clean-growth | — | — | 0 | 0 | 1.0 |
| h3-recent-chapter-rule | flying 3/3 | flying 3/3 | 0 | 0 | 1.0 |

## Findings

### h3-first-ship · 첫 배(앞 회차의 사실을 건너는 모순)
- run 1: 0 found, 0 unverified, 0 kept, 4.8s
- run 2: 0 found, 0 unverified, 0 kept, 4.8s
- run 3: 0 found, 0 unverified, 0 kept, 4.5s

### h3-dead-sister · 죽은 언니와 회상 뒤의 현재
- run 1: 1 found, 0 unverified, 1 kept, 13.0s
  - [expect:sister-back] (HIGH) “오늘 아침, 세린은 공방 문을 열고 들어와 오나의 작업대 옆에 앉았다.” ← c1: “오나의 언니 세린은 지난겨울 병으로 세상을 떠났다.”
    1화 「장례」에서는 세린이 지난겨울 병으로 세상을 떠났다고 정해요. 그런데 2화 「공방」에서는 오늘 아침 세린이 공방에 들어와 오나의 작업대 옆에 앉았다고 서술해요. 세상을 떠난 세린이 현재 공방에 와 있는 두 내용은 함께 성립하기 어려운데, 그 사이에 세린이 돌아온 경위나 이 장면이 환상이라는 설명은 나오지 않아요.
- run 2: 1 found, 0 unverified, 1 kept, 13.3s
  - [expect:sister-back] (HIGH) “오늘 아침, 세린은 공방 문을 열고 들어와 오나의 작업대 옆에 앉았다.” ← c1: “오나의 언니 세린은 지난겨울 병으로 세상을 떠났다.”
    1화 「장례」에서는 오나의 언니 세린이 지난겨울 병으로 세상을 떠났다고 해요. 그런데 2화에서 오늘 아침 세린이 공방에 들어와 오나의 작업대 옆에 앉았다고 서술해요. 세상을 떠난 세린이 오늘 아침 직접 찾아온 일은 두 서술과 동시에 성립할 수 없어요. 세린이 살아 돌아온 이유나 이 장면이 환상·회상이라는 설명은 원고에 나오지 않아요.
- run 3: 1 found, 0 unverified, 1 kept, 13.7s
  - [expect:sister-back] (HIGH) “오늘 아침, 세린은 공방 문을 열고 들어와 오나의 작업대 옆에 앉았다.” ← c1: “오나의 언니 세린은 지난겨울 병으로 세상을 떠났다.”
    1화는 오나의 언니 세린이 지난겨울 병으로 세상을 떠났다고 밝혀요. 그런데 2화에서는 오늘 아침 세린이 공방에 들어와 오나의 작업대 옆에 앉았다고 서술해요. 사망한 세린이 현재 공방에 와서 앉았다는 두 내용은 그대로는 함께 성립할 수 없어요. 세린이 돌아오거나 다시 나타날 수 있게 된 사건이나 이유는 원고에 나오지 않아요.

### h3-alias-age · 직함으로 부른 인물의 나이
- run 1: 2 found, 1 unverified, 1 kept, 19.9s
  - [expect:young-mayor] (HIGH) “갓 스무 살이 된 촌장이 축제의 첫 춤을 추었다.” ← mayor: “바닷가 마을의 촌장. 일흔 살 노인이다.”
    하윤 설정은 바닷가 마을 촌장이 일흔 살이라고 정해 두었어요. 그런데 2화 축제 장면에서는 촌장이 갓 스무 살이 되었다고 나와요. 같은 촌장의 나이가 같은 시점에 함께 성립할 수 없고, 나이가 바뀌었거나 다른 촌장이라는 설명도 원고에 없어요.
- run 2: 2 found, 1 unverified, 1 kept, 20.6s
  - [expect:young-mayor] (HIGH) “갓 스무 살이 된 촌장이 축제의 첫 춤을 추었다.” ← mayor: “일흔 살 노인이다.”
    하윤 설정은 ‘촌장’이 바닷가 마을의 촌장이며 일흔 살 노인이라고 정해 두었어요. 그런데 2화 축제 장면에서는 촌장이 갓 스무 살이 되었다고 서술해요. 같은 시점의 촌장이 일흔 살이면서 갓 스무 살일 수는 없고, 나이가 달라진 사정도 원고에 나오지 않아요.
- run 3: 2 found, 1 unverified, 1 kept, 20.6s
  - [expect:young-mayor] (HIGH) “갓 스무 살이 된 촌장이 축제의 첫 춤을 추었다.” ← mayor: “일흔 살 노인이다.”
    하윤 설정은 촌장을 일흔 살 노인으로 정해 두었어요. 그런데 2화에서는 촌장이 갓 스무 살이 됐다고 서술해요. 시간 이동이나 나이가 달라진 사정이 나오지 않아, 같은 시점의 촌장이 두 나이일 수는 없어요.

### h3-clean-growth · 이유가 나온 성장
- run 1: 0 found, 0 unverified, 0 kept, 23.6s
- run 2: 0 found, 0 unverified, 0 kept, 23.2s
- run 3: 0 found, 0 unverified, 0 kept, 22.8s

### h3-recent-chapter-rule · 앞 회차가 정한 규칙
- run 1: 1 found, 0 unverified, 1 kept, 28.9s
  - [expect:flying] (HIGH) “첫 수업이 끝나자 리오는 학원 안뜰 위를 날아 기숙사 창문으로 들어갔다.” ← c1: “학원 안에서는 누구도 날 수 없다.”
    1화 「입학」에서 학장은 탑의 결계가 비행 마법을 막아 학원 안에서는 누구도 날 수 없다고 말해요. 그런데 2화에서 리오는 학원 안뜰 위를 날아 기숙사 창문으로 들어가요. 두 내용은 같은 시점의 학원 안 비행을 두고 서로 양립할 수 없고, 그 사이 결계가 사라지거나 바뀌었다는 내용도 없어요.
- run 2: 1 found, 0 unverified, 1 kept, 30.6s
  - [expect:flying] (HIGH) “첫 수업이 끝나자 리오는 학원 안뜰 위를 날아 기숙사 창문으로 들어갔다.” ← c1: “학원 안에서는 누구도 날 수 없다.”
    1화 「입학」에서 학장은 탑의 결계가 비행 마법을 막기 때문에 학원 안에서는 누구도 날 수 없다고 말해요. 그런데 2화에서 리오는 학원 안뜰 위를 날아 기숙사 창문으로 들어가요. 학원 안에서 비행이 불가능하다는 말과 리오가 그 안에서 날았다는 장면은 함께 성립하지 않아요. 그 사이에 결계가 바뀌거나 비행을 가능하게 하는 일이 있었다는 설명은 없어요.
- run 3: 1 found, 0 unverified, 1 kept, 29.8s
  - [expect:flying] (HIGH) “첫 수업이 끝나자 리오는 학원 안뜰 위를 날아 기숙사 창문으로 들어갔다.” ← c1: “입학식에서 학장은 말했다. 학원 안에서는 누구도 날 수 없다.”
    1화에서 학장은 탑의 결계가 비행 마법을 막기 때문에 학원 안에서는 누구도 날 수 없다고 말해요. 그런데 2화에서는 첫 수업이 끝난 뒤 리오가 학원 안뜰 위를 날아 기숙사 창문으로 들어가요. 학원 안에서 비행이 불가능하다는 말과 리오가 그 안에서 나는 일은 이 시점에 함께 성립할 수 없어요. 그 사이 결계가 풀리거나 리오에게 예외가 생겼다는 설명은 나오지 않아요.

