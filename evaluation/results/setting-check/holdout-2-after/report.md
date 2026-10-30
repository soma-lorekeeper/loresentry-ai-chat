# Setting check evaluation

- Runs per case: 3
- Recall: 0.833 (stable in every run: 0.75)
- Trap rate (reported something it must not): 0.0
- Precision (findings that are expected or acceptable): 1.0
- Unlisted findings per run: 0.0
- Run-to-run agreement (Jaccard): 0.89
- Median seconds per check: 31.8
- Tokens: input 51605, cached 28483, output 10451

| Case | Expected (hits/runs) | Finder saw | Traps hit | Unlisted | Agreement |
| --- | --- | --- | --- | --- | --- |
| h2-oldest-tree | older-pine 3/3 | older-pine 3/3 | 0 | 0 | 1.0 |
| h2-only-bridge | broken-bridge 1/3 | broken-bridge 3/3 | 0 | 0 | 0.33 |
| h2-rumor-then-fact | speech 3/3 | speech 3/3 | 0 | 0 | 1.0 |
| h2-lie-in-dialogue | — | — | 0 | 0 | 1.0 |
| h2-season-pass | — | — | 0 | 0 | 1.0 |
| h2-core-color | red-lion 3/3 | red-lion 3/3 | 0 | 0 | 1.0 |

## Findings

### h2-oldest-tree · 가장 오래된 나무(앞 회차의 나이를 건너는 모순)
- run 1: 1 found, 0 unverified, 1 kept, 12.4s
  - [expect:older-pine] (HIGH) “숲 북쪽 끝의 검은 소나무는 천 년 전 숲이 처음 생길 때부터 그 자리에 서 있었다.” ← oak: “숲에서 가장 오래된 나무.”
    ‘은잎 참나무’ 설정은 은잎 참나무가 숲에서 가장 오래된 나무라고 정해 두었고, 1화에서는 그 나무가 삼백 살이라고 밝혀요. 그런데 2화는 검은 소나무가 천 년 전 숲이 생길 때부터 그 자리에 있었다고 해요. 두 나무가 같은 숲에 있는 만큼, 검은 소나무가 은잎 참나무보다 오래되므로 두 내용은 함께 성립할 수 없어요. 그 사이 은잎 참나무가 더 오래되었거나 검은 소나무가 그 자리에 없었다는 변화는 원고에 나오지 않아요.
- run 2: 2 found, 1 unverified, 1 kept, 13.6s
  - [expect:older-pine] (HIGH) “숲 북쪽 끝의 검은 소나무는 천 년 전 숲이 처음 생길 때부터 그 자리에 서 있었다.” ← oak: “숲에서 가장 오래된 나무.”
    ‘은잎 참나무’ 설정은 은잎 참나무가 숲에서 가장 오래된 나무라고 정해 두었어요. 그런데 2화에서는 검은 소나무가 숲이 처음 생길 때부터 그 자리에 있었다고 해요. 숲의 시작부터 서 있던 검은 소나무보다 은잎 참나무가 더 오래되었다고 볼 근거가 없고, 두 나무의 나이를 달리 설명하는 내용도 나오지 않아요.
- run 3: 1 found, 0 unverified, 1 kept, 12.0s
  - [expect:older-pine] (HIGH) “숲 북쪽 끝의 검은 소나무는 천 년 전 숲이 처음 생길 때부터 그 자리에 서 있었다.” ← oak: “숲에서 가장 오래된 나무.”
    ‘은잎 참나무’ 설정은 은잎 참나무가 숲에서 가장 오래된 나무라고 정해 두었고, 1화에서는 그 나이가 삼백 살이라고 밝혀요. 그런데 2화에서 검은 소나무는 숲이 처음 생긴 천 년 전부터 그 자리에 있었다고 해요. 따라서 검은 소나무가 은잎 참나무보다 훨씬 오래된 셈이라 두 내용은 함께 성립할 수 없고, 그 사이에 숲의 나이나 두 나무의 관계가 달라졌다는 내용도 나오지 않아요.

### h2-only-bridge · 유일한 다리(앞 회차를 건너는 모순)
- run 1: 1 found, 0 unverified, 1 kept, 22.1s
  - [expect:broken-bridge] (HIGH) “같은 봄, 사냥꾼 둘은 흰 강의 돌다리를 걸어서 건너 마을로 돌아왔다.” ← c1: “봄 홍수에 흰 강의 돌다리가 무너졌다. 마을 사람들은 여름까지 배로 강을 건넜다.”
    1화에서는 봄 홍수로 흰 강의 돌다리가 무너졌고, 마을 사람들이 여름까지 배로 강을 건넜다고 해요. 그런데 2화는 같은 봄에 사냥꾼 둘이 그 돌다리를 걸어서 건넜다고 서술해요. 무너진 다리와 걸어서 건널 수 있는 다리는 같은 시점에 함께 성립할 수 없고, 그 사이 다리가 복구되었다는 내용도 없어요.
- run 2: 1 found, 0 unverified, 0 kept, 21.1s; judge dropped: broken-bridge→INSUFFICIENT
- run 3: 1 found, 0 unverified, 0 kept, 23.8s; judge dropped: broken-bridge→INSUFFICIENT

### h2-rumor-then-fact · 소문 다음의 서술
- run 1: 2 found, 1 unverified, 1 kept, 28.6s
  - [expect:speech] (HIGH) “그날 정오, 노을은 광장 한가운데에서 또렷한 목소리로 연설을 했다.” ← mute: “태어날 때부터 말을 하지 못한다.”
    ‘노을’ 설정은 노을이 태어날 때부터 말을 하지 못한다고 정해 두었어요. 그런데 2화에서 노을은 정오에 광장에서 또렷한 목소리로 연설해요. 말을 하지 못하는 노을이 목소리로 연설하는 두 사실은 같은 시점에 함께 성립할 수 없어요. 그 사이 노을이 말을 할 수 있게 되었다는 사건이나 까닭은 나오지 않아요.
- run 2: 2 found, 1 unverified, 1 kept, 29.5s
  - [expect:speech] (HIGH) “그날 정오, 노을은 광장 한가운데에서 또렷한 목소리로 연설을 했다.” ← mute: “태어날 때부터 말을 하지 못한다.”
    ‘노을’ 설정은 노을이 태어날 때부터 말을 하지 못한다고 정해 두었어요. 그런데 2화에서 노을은 광장에서 또렷한 목소리로 연설해요. 두 내용은 그 시점에 함께 성립할 수 없고, 2화에는 노을이 말을 할 수 있게 된 계기나 변화가 나오지 않아요.
- run 3: 2 found, 1 unverified, 1 kept, 31.8s
  - [expect:speech] (HIGH) “그날 정오, 노을은 광장 한가운데에서 또렷한 목소리로 연설을 했다.” ← mute: “태어날 때부터 말을 하지 못한다.”
    ‘노을’ 설정은 노을이 태어날 때부터 말을 하지 못한다고 정해 두었어요. 그런데 2화에서는 노을이 광장에서 또렷한 목소리로 연설했다고 서술해요. 말을 하지 못하는 노을이 그 시점에 목소리로 연설한 두 내용은 함께 성립할 수 없고, 원고에는 노을이 말할 수 있게 된 계기나 변화가 나오지 않아요.

### h2-lie-in-dialogue · 대사 속 거짓말
- run 1: 0 found, 0 unverified, 0 kept, 30.3s
- run 2: 0 found, 0 unverified, 0 kept, 31.8s
- run 3: 0 found, 0 unverified, 0 kept, 32.2s

### h2-season-pass · 계절이 지나 바뀐 것
- run 1: 0 found, 0 unverified, 0 kept, 34.2s
- run 2: 0 found, 0 unverified, 0 kept, 33.6s
- run 3: 0 found, 0 unverified, 0 kept, 34.1s

### h2-core-color · 중요 설정의 색
- run 1: 3 found, 2 unverified, 1 kept, 41.8s
  - [expect:red-lion] (HIGH) “붉은 바탕에 금빛 사자가 그려진 왕가의 깃발을 앞세우고 군대가 성문을 나섰다.” ← banner: “왕가의 깃발은 푸른 바탕에 흰 사슴이다.”
    ‘왕가의 깃발’ 설정은 깃발이 푸른 바탕에 흰 사슴이라고 정해 두었어요. 그런데 2화에서 군대는 붉은 바탕에 금빛 사자가 그려진 왕가의 깃발을 앞세워 성문을 나서요. 같은 왕가의 깃발이 같은 시점에 두 가지 색과 문양을 가질 수는 없고, 두 깃발이 따로 있거나 설정이 바뀌었다는 설명도 없어요.
- run 2: 2 found, 1 unverified, 1 kept, 45.1s
  - [expect:red-lion] (HIGH) “붉은 바탕에 금빛 사자가 그려진 왕가의 깃발을 앞세우고 군대가 성문을 나섰다.” ← banner: “왕가의 깃발은 푸른 바탕에 흰 사슴이다.”
    ‘왕가의 깃발’ 설정은 깃발이 푸른 바탕에 흰 사슴이라고 정해 두었어요. 그런데 2화 행진 장면에서는 군대가 붉은 바탕에 금빛 사자가 그려진 왕가의 깃발을 앞세워 성문을 나서요. 같은 왕가의 깃발을 가리키므로 두 무늬는 함께 성립할 수 없고, 깃발의 무늬가 바뀌었거나 다른 깃발을 왕가의 깃발이라 부르는 경위도 나오지 않아요.
- run 3: 3 found, 2 unverified, 1 kept, 44.5s
  - [expect:red-lion] (HIGH) “붉은 바탕에 금빛 사자가 그려진 왕가의 깃발을 앞세우고 군대가 성문을 나섰다.” ← banner: “왕가의 깃발은 푸른 바탕에 흰 사슴이다.”
    ‘왕가의 깃발’ 설정은 그 깃발이 푸른 바탕에 흰 사슴이라고 정해 두었어요. 그런데 2화에서 군대가 앞세운 왕가의 깃발은 붉은 바탕에 금빛 사자가 그려져 있다고 서술해요. 같은 왕가의 깃발이 한 시점에 두 가지 모습일 수는 없고, 두 깃발이 따로 있거나 깃발이 바뀌었다는 내용도 나오지 않아요.

