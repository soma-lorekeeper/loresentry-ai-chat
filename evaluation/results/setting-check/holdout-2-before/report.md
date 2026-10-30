# Setting check evaluation

- Runs per case: 3
- Recall: 0.75 (stable in every run: 0.75)
- Trap rate (reported something it must not): 0.0
- Precision (findings that are expected or acceptable): 1.0
- Unlisted findings per run: 0.0
- Run-to-run agreement (Jaccard): 1.0
- Median seconds per check: 16.3
- Tokens: input 33240, cached 0, output 7028

| Case | Expected (hits/runs) | Finder saw | Traps hit | Unlisted | Agreement |
| --- | --- | --- | --- | --- | --- |
| h2-oldest-tree | older-pine 0/3 | older-pine 3/3 | 0 | 0 | 1.0 |
| h2-only-bridge | broken-bridge 3/3 | broken-bridge 3/3 | 0 | 0 | 1.0 |
| h2-rumor-then-fact | speech 3/3 | speech 3/3 | 0 | 0 | 1.0 |
| h2-lie-in-dialogue | — | — | 0 | 0 | 1.0 |
| h2-season-pass | — | — | 0 | 0 | 1.0 |
| h2-core-color | red-lion 3/3 | red-lion 3/3 | 0 | 0 | 1.0 |

## Findings

### h2-oldest-tree · 가장 오래된 나무(앞 회차의 나이를 건너는 모순)
- run 1: 1 found, 0 unverified, 0 kept, 6.3s; judge dropped: older-pine→INSUFFICIENT
- run 2: 1 found, 0 unverified, 0 kept, 8.9s; judge dropped: older-pine→INSUFFICIENT
- run 3: 1 found, 0 unverified, 0 kept, 7.9s; judge dropped: older-pine→INSUFFICIENT

### h2-only-bridge · 유일한 다리(앞 회차를 건너는 모순)
- run 1: 1 found, 0 unverified, 1 kept, 7.5s
  - [expect:broken-bridge] (HIGH) “같은 봄, 사냥꾼 둘은 흰 강의 돌다리를 걸어서 건너 마을로 돌아왔다.” ← c1: “봄 홍수에 흰 강의 돌다리가 무너졌다.”
    1화 「홍수」에서는 봄 홍수로 흰 강의 돌다리가 무너졌다고 정해요. 그런데 2화에서 ‘같은 봄’에 사냥꾼 둘이 그 돌다리를 걸어서 건너 마을로 돌아와요. 무너진 다리가 걸어서 건널 수 있는 상태일 수는 없는데, 1화 이후 다리가 복구되거나 다시 놓이는 일은 나오지 않아 두 내용이 함께 성립하지 않아요.
- run 2: 1 found, 0 unverified, 1 kept, 13.6s
  - [expect:broken-bridge] (HIGH) “같은 봄, 사냥꾼 둘은 흰 강의 돌다리를 걸어서 건너 마을로 돌아왔다.” ← c1: “봄 홍수에 흰 강의 돌다리가 무너졌다.”
    1화 「홍수」에서는 봄 홍수로 흰 강의 돌다리가 무너졌다고 정해요. 그런데 2화에서 사냥꾼 둘은 같은 봄에 그 돌다리를 걸어서 건너 마을로 돌아와요. 무너진 다리는 걸어서 건널 수 없고, 두 장면 사이에 다리가 복구되거나 다시 놓이는 일도 나오지 않아 두 내용은 함께 성립할 수 없어요.
- run 3: 1 found, 0 unverified, 1 kept, 13.9s
  - [expect:broken-bridge] (HIGH) “같은 봄, 사냥꾼 둘은 흰 강의 돌다리를 걸어서 건너 마을로 돌아왔다.” ← c1: “봄 홍수에 흰 강의 돌다리가 무너졌다.”
    1화 「홍수」는 봄 홍수로 흰 강의 돌다리가 무너졌다고 정해 두었어요. 그런데 2화에서는 같은 봄에 사냥꾼 둘이 그 돌다리를 걸어서 건너 마을로 돌아와요. 같은 시기에 다리가 무너진 상태와 두 사람이 걸어서 건넜다는 사실은 함께 성립할 수 없어요. 2화까지 다리가 다시 놓이거나 복구된 일은 나오지 않아요.

### h2-rumor-then-fact · 소문 다음의 서술
- run 1: 1 found, 0 unverified, 1 kept, 13.7s
  - [expect:speech] (HIGH) “그날 정오, 노을은 광장 한가운데에서 또렷한 목소리로 연설을 했다.” ← mute: “태어날 때부터 말을 하지 못한다.”
    ‘노을’ 설정은 노을이 태어날 때부터 말을 하지 못한다고 정해 두었어요. 그런데 2화에서 노을은 광장에서 또렷한 목소리로 연설해요. 두 내용은 같은 시점에 함께 성립할 수 없고, 노을이 말을 할 수 있게 된 계기나 설정이 바뀌었다는 내용도 없어요.
- run 2: 1 found, 0 unverified, 1 kept, 15.3s
  - [expect:speech] (HIGH) “그날 정오, 노을은 광장 한가운데에서 또렷한 목소리로 연설을 했다.” ← mute: “태어날 때부터 말을 하지 못한다.”
    ‘노을’ 설정은 노을이 태어날 때부터 말을 하지 못하고 손짓으로 뜻을 전한다고 정해 두었어요. 그런데 2화에서 노을은 정오에 광장 한가운데서 목소리로 연설해요. 태어날 때부터 말을 하지 못한다는 설정과 실제로 목소리를 내어 연설하는 장면은 함께 성립할 수 없고, 노을이 말할 수 있게 된 계기도 나오지 않아요.
- run 3: 1 found, 0 unverified, 1 kept, 19.5s
  - [expect:speech] (HIGH) “그날 정오, 노을은 광장 한가운데에서 또렷한 목소리로 연설을 했다.” ← mute: “태어날 때부터 말을 하지 못한다.”
    ‘노을’ 설정은 노을이 태어날 때부터 말을 하지 못하고 손짓으로 뜻을 전한다고 정해 두었어요. 그런데 2화에서 노을은 광장 한가운데서 또렷한 목소리로 연설해요. 말하지 못하는 노을이 그 시점에 직접 목소리로 연설할 수는 없고, 이를 가능하게 하는 변화나 다른 설명도 원고에 나오지 않아요.

### h2-lie-in-dialogue · 대사 속 거짓말
- run 1: 0 found, 0 unverified, 0 kept, 15.8s
- run 2: 0 found, 0 unverified, 0 kept, 16.3s
- run 3: 0 found, 0 unverified, 0 kept, 17.1s

### h2-season-pass · 계절이 지나 바뀐 것
- run 1: 0 found, 0 unverified, 0 kept, 17.3s
- run 2: 0 found, 0 unverified, 0 kept, 17.8s
- run 3: 0 found, 0 unverified, 0 kept, 18.8s

### h2-core-color · 중요 설정의 색
- run 1: 3 found, 2 unverified, 1 kept, 25.5s
  - [expect:red-lion] (HIGH) “붉은 바탕에 금빛 사자가 그려진 왕가의 깃발을 앞세우고 군대가 성문을 나섰다.” ← banner: “왕가의 깃발은 푸른 바탕에 흰 사슴이다.”
    ‘왕가의 깃발’ 설정은 깃발이 푸른 바탕에 흰 사슴이라고 정해 두었어요. 그런데 2화 행진 장면에서는 군대가 붉은 바탕에 금빛 사자가 그려진 왕가의 깃발을 앞세우고 성문을 나서요. 같은 왕가의 깃발이 두 가지 서로 다른 색과 문양을 가질 수는 없고, 두 깃발이 따로 있거나 설정이 바뀌었다는 설명도 없어요.
- run 2: 3 found, 2 unverified, 1 kept, 25.9s
  - [expect:red-lion] (HIGH) “붉은 바탕에 금빛 사자가 그려진 왕가의 깃발을 앞세우고 군대가 성문을 나섰다.” ← banner: “왕가의 깃발은 푸른 바탕에 흰 사슴이다.”
    ‘왕가의 깃발’ 설정은 깃발이 푸른 바탕에 흰 사슴이라고 정해 두었어요. 그런데 2화에서 군대가 앞세운 왕가의 깃발은 붉은 바탕에 금빛 사자로 묘사돼요. 같은 왕가의 깃발이 같은 시점에 두 가지 바탕색과 문장을 가질 수 없고, 깃발이 바뀌었거나 별도의 깃발이라는 내용도 없어요.
- run 3: 3 found, 2 unverified, 1 kept, 27.1s
  - [expect:red-lion] (HIGH) “붉은 바탕에 금빛 사자가 그려진 왕가의 깃발을 앞세우고 군대가 성문을 나섰다.” ← banner: “왕가의 깃발은 푸른 바탕에 흰 사슴이다.”
    ‘왕가의 깃발’ 설정은 왕가의 깃발이 푸른 바탕에 흰 사슴이라고 정해 두었어요. 그런데 2화에서는 군대가 성문을 나설 때 왕가의 깃발에 붉은 바탕과 금빛 사자가 있다고 보여 줘요. 같은 깃발의 바탕색과 문양이 서로 달라 두 내용은 함께 성립할 수 없고, 깃발이 바뀌었다는 사건이나 이유도 원고에 나오지 않아요.

