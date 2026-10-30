# Setting check evaluation

- Runs per case: 3
- Recall: 0.771 (stable in every run: 0.562)
- Trap rate (reported something it must not): 0.0
- Precision (findings that are expected or acceptable): 1.0
- Unlisted findings per run: 0.0
- Run-to-run agreement (Jaccard): 0.76
- Median seconds per check: 39.3
- Tokens: input 286052, cached 157211, output 30889

| Case | Expected (hits/runs) | Finder saw | Traps hit | Unlisted | Agreement |
| --- | --- | --- | --- | --- | --- |
| glass-garden-7-planted | silver-key 0/3, notebook-giver 1/3, lena-lost 3/3 | silver-key 3/3, notebook-giver 3/3, lena-lost 3/3 | 0 | 0 | 0.67 |
| starlight-rule | noon-magic 3/3, kyle-swims 3/3 | noon-magic 3/3, kyle-swims 3/3 | 0 | 0 | 1.0 |
| numbers-across-chapters | age-jump 3/3, tower-count 3/3 | age-jump 3/3, tower-count 3/3 | 0 | 0 | 1.0 |
| dead-man | alive-again 2/3 | alive-again 3/3 | 0 | 0 | 0.33 |
| alias-family | brother-grave 2/3 | brother-grave 3/3 | 0 | 0 | 0.33 |
| lighthouse-place | west-cliff 2/3, light-at-two 3/3 | west-cliff 2/3, light-at-two 3/3 | 0 | 0 | 0.67 |
| appearance | long-silver-hair 3/3 | long-silver-hair 3/3 | 0 | 0 | 1.0 |
| clean-exception | — | — | 0 | 0 | 1.0 |
| seal-explained | — | — | 0 | 0 | 1.0 |
| seal-unexplained | commoner-seal 1/3 | commoner-seal 3/3 | 0 | 0 | 0.33 |
| english-letters | reads-aloud 3/3 | reads-aloud 3/3 | 0 | 0 | 1.0 |
| glass-garden-8-sea | lena-boards 2/3 | lena-boards 3/3 | 0 | 0 | 0.33 |
| spring-table-18-years | five-years 3/3 | five-years 3/3 | 0 | 0 | 1.0 |
| fog-harbor-13-tea | — | — | 0 | 0 | 1.0 |

## Findings

### glass-garden-7-planted · 예시 작품 7화에 심은 오류(prod 테스트 프로젝트와 같은 글)
- run 1: 6 found, 3 unverified, 1 kept, 23.4s; judge dropped: silver-key→INSUFFICIENT, notebook-giver→INSUFFICIENT
  - [expect:lena-lost] (HIGH) “그 시각 항구에서 올라오던 레나는 짙은 안개 속에서 길을 잃고 새벽까지 정원 밖을 헤맸다.” ← lena: “짙은 안개 속에서도 그는 한 번도 길을 잃지 않았다.”
    「레나 아르벨」 설정은 레나가 짙은 안개 속에서도 한 번도 길을 잃지 않는다고 정해 두었어요. 그런데 7화에서는 항구에서 오던 레나가 짙은 안개 속에서 길을 잃고 새벽까지 정원 밖을 헤맨다고 나와요. 같은 안개 속에서 길을 잃지 않는 레나와 길을 잃은 레나는 동시에 성립할 수 없고, 그 능력이 달라졌거나 이번에는 작동하지 않은 이유도 제시되지 않아요.
- run 2: 4 found, 1 unverified, 1 kept, 21.6s; judge dropped: silver-key→INSUFFICIENT, notebook-giver→INSUFFICIENT
  - [expect:lena-lost] (HIGH) “그 시각 항구에서 올라오던 레나는 짙은 안개 속에서 길을 잃고 새벽까지 정원 밖을 헤맸다.” ← lena: “짙은 안개 속에서도 그는 한 번도 길을 잃지 않았다.”
    ‘레나 아르벨’ 설정은 레나가 짙은 안개 속에서도 한 번도 길을 잃지 않는다고 정해 두었어요. 그런데 7화에서는 레나가 짙은 안개 속에서 길을 잃고 새벽까지 정원 밖을 헤맨다고 서술해요. 같은 안개 속에서 레나가 길을 잃지 않는다는 설정과 길을 잃었다는 서술은 함께 성립할 수 없고, 이 장에는 그 능력이 달라졌거나 작동하지 않은 이유가 나오지 않아요.
- run 3: 4 found, 1 unverified, 2 kept, 18.9s; judge dropped: silver-key→INSUFFICIENT
  - [expect:notebook-giver] (HIGH) “서윤은 처음 정원에 온 날 테오에게서 받은 기록장을 펼쳐, 문 앞의 발자국을 옮겨 적었다.” ← harin: “서윤에게 기록장을 건넨 사람이며,”
    ‘하린’ 설정은 서윤에게 기록장을 건넨 사람이 하린이라고 정해 두었어요. 그런데 7화에서는 서윤이 처음 정원에 온 날 테오에게서 그 기록장을 받았다고 밝혀요. 같은 기록장을 서윤에게 건넨 사람이 한쪽에서는 하린, 다른 쪽에서는 테오이므로 두 내용은 함께 성립할 수 없어요. 두 사람이 함께 건넸거나 전달 경위가 달랐다는 설명은 원고에 나오지 않아요.
  - [expect:lena-lost] (HIGH) “그 시각 항구에서 올라오던 레나는 짙은 안개 속에서 길을 잃고 새벽까지 정원 밖을 헤맸다.” ← lena: “짙은 안개 속에서도 그는 한 번도 길을 잃지 않았다.”
    ‘레나 아르벨’ 설정은 레나가 짙은 안개 속에서도 한 번도 길을 잃지 않는다고 정해 두었어요. 그런데 7화에서는 레나가 짙은 안개 속에서 길을 잃고 새벽까지 정원 밖을 헤매요. 같은 안개 속에서 길을 잃지 않는다는 설정과 길을 잃었다는 장면은 함께 성립할 수 없고, 이 장면을 꿈이나 착각으로 볼 근거도 없어요.

### starlight-rule · 마법 규칙과 인물 약점
- run 1: 3 found, 1 unverified, 2 kept, 10.4s
  - [expect:noon-magic] (HIGH) “이리스는 소매를 걷어붙이고 별빛 마법으로 불길을 단숨에 잠재운 뒤, 땀 한 방울 흘리지 않고 다음 골목으로 걸어갔다.” ← magic: “별빛 마법은 해가 진 뒤에만 쓸 수 있다. 해가 떠 있을 때 쓰면 시전자가 그 자리에서 쓰러진다.”
    ‘별빛 마법’ 설정은 해가 떠 있을 때 사용하면 시전자가 그 자리에서 쓰러진다고 정해 두었어요. 그런데 2화에서 한낮에 이리스가 그 마법으로 불길을 잠재운 뒤 다음 골목으로 걸어가요. 한낮에 마법을 쓴 이리스가 쓰러지지 않고 걸어가는 모습은 설정과 함께 성립할 수 없고, 원고에는 이 결과를 달리 설명할 사건이나 이유가 없어요.
  - [expect:kyle-swims] (HIGH) “카일은 망설임 없이 강물에 뛰어들어 맞은편 기슭까지 헤엄쳐 아이를 건져 냈다.” ← kyle: “어릴 적 강에 빠진 뒤로 물을 무서워해 헤엄을 치지 못한다.”
    「카일」 설정은 카일이 어릴 적 강에 빠진 뒤 물을 무서워해 헤엄을 치지 못한다고 정해 두었어요. 그런데 2화에서 카일은 강물에 뛰어들어 맞은편 기슭까지 헤엄쳐 아이를 구해요. 그 시점에 헤엄을 익혔거나 두려움을 극복했다는 일이 나오지 않아 두 내용은 함께 성립할 수 없어요.
- run 2: 3 found, 1 unverified, 2 kept, 12.5s
  - [expect:noon-magic] (HIGH) “한낮의 광장에 불길이 번졌다. 이리스는 소매를 걷어붙이고 별빛 마법으로 불길을 단숨에 잠재운 뒤, 땀 한 방울 흘리지 않고 다음 골목으로 걸어갔다.” ← magic: “별빛 마법은 해가 진 뒤에만 쓸 수 있다. 해가 떠 있을 때 쓰면 시전자가 그 자리에서 쓰러진다.”
    ‘별빛 마법’ 설정은 별빛 마법을 해가 진 뒤에만 쓸 수 있고, 낮에 쓰면 시전자가 그 자리에서 쓰러진다고 정해 두었어요. 그런데 2화에서 한낮에 이리스가 별빛 마법으로 불을 끈 뒤 다음 골목으로 걸어가요. 이리스가 낮에 마법을 썼으면서 쓰러지지 않았으므로 두 내용은 함께 성립할 수 없어요. 낮에도 쓸 수 있게 된 까닭이나 쓰러짐을 피한 방법은 원고에 나오지 않아요.
  - [expect:kyle-swims] (HIGH) “카일은 망설임 없이 강물에 뛰어들어 맞은편 기슭까지 헤엄쳐 아이를 건져 냈다.” ← kyle: “어릴 적 강에 빠진 뒤로 물을 무서워해 헤엄을 치지 못한다.”
    「카일」 설정은 카일이 어릴 적 강에 빠진 뒤 물을 무서워해 헤엄을 치지 못한다고 정해 두었어요. 그런데 2화에서 그는 강물에 뛰어들어 맞은편 기슭까지 헤엄쳐 아이를 구해요. 헤엄을 치지 못한다는 설정과 실제로 강을 헤엄쳐 건넌 행동은 그 시점에 함께 성립할 수 없어요. 카일이 헤엄치는 법을 익혔다는 장면이나 달리 헤엄칠 수 있게 된 까닭은 나오지 않아요.
- run 3: 3 found, 1 unverified, 2 kept, 9.6s
  - [expect:noon-magic] (HIGH) “한낮의 광장에 불길이 번졌다. 이리스는 소매를 걷어붙이고 별빛 마법으로 불길을 단숨에 잠재운 뒤, 땀 한 방울 흘리지 않고 다음 골목으로 걸어갔다.” ← magic: “별빛 마법은 해가 진 뒤에만 쓸 수 있다. 해가 떠 있을 때 쓰면 시전자가 그 자리에서 쓰러진다.”
    ‘별빛 마법’ 설정은 별빛 마법을 해가 진 뒤에만 쓸 수 있고, 낮에 쓰면 시전자가 그 자리에서 쓰러진다고 정해 두었어요. 그런데 2화에서 한낮에 이리스가 별빛 마법으로 불길을 끈 뒤 다음 골목으로 걸어가요. 낮에 마법을 썼는데도 쓰러지지 않은 모습은 설정과 양립할 수 없고, 이리스가 쓰러지지 않은 이유나 예외가 이야기에서 제시되지 않아요.
  - [expect:kyle-swims] (HIGH) “카일은 망설임 없이 강물에 뛰어들어 맞은편 기슭까지 헤엄쳐 아이를 건져 냈다.” ← kyle: “어릴 적 강에 빠진 뒤로 물을 무서워해 헤엄을 치지 못한다.”
    ‘카일’ 설정은 카일이 어릴 적 강에 빠진 뒤 물을 무서워해 헤엄을 치지 못한다고 정해 두었어요. 그런데 2화에서 카일은 강물에 뛰어들어 맞은편 기슭까지 헤엄쳐 아이를 구해요. 헤엄을 치지 못하는 사람은 이 장면에서 헤엄쳐 건널 수 없어 두 내용이 함께 성립하지 않아요. 카일이 수영을 배우거나 다른 방법으로 강을 건너게 된 계기는 원고에 나오지 않아요.

### numbers-across-chapters · 앞 회차의 숫자와 시간
- run 1: 2 found, 0 unverified, 2 kept, 21.0s
  - [expect:age-jump] (HIGH) “견습 기사가 된 지 한 달 만에, 에단은 열여덟 번째 생일을 맞았다.” ← c1: “에단은 열여섯 살 봄에 견습 기사가 되었다.”
    1화 「견습 기사」에서는 에단이 열여섯 살 봄에 견습 기사가 되었다고 정해요. 그런데 3화에서는 견습 기사가 된 지 한 달 만에 열여덟 번째 생일을 맞았다고 해요. 입문 후 한 달 만에 열여섯 살에서 열여덟 살이 될 수는 없고, 그 사이에 시간이 더 흘렀다는 설명도 없어서 두 내용은 함께 성립하지 않아요.
  - [expect:tower-count] (HIGH) “망루는 그것이 전부였다.” ← c1: “회색 성의 성벽에는 망루가 모두 일곱 개 있었다.”
    1화 「견습 기사」에서는 회색 성 성벽의 망루가 모두 일곱 개라고 정해요. 그런데 3화에서는 그날 밤 횃불을 올린 다섯 개가 망루의 전부라고 해요. 같은 성의 망루가 일곱 개이면서 다섯 개뿐일 수는 없고, 그사이 망루가 사라졌다는 사건도 나오지 않아요.
- run 2: 2 found, 0 unverified, 2 kept, 20.8s
  - [expect:age-jump] (HIGH) “견습 기사가 된 지 한 달 만에, 에단은 열여덟 번째 생일을 맞았다.” ← c1: “에단은 열여섯 살 봄에 견습 기사가 되었다.”
    1화 「견습 기사」에서는 에단이 열여섯 살 봄에 견습 기사가 되었다고 정해요. 그런데 3화에서는 견습 기사가 된 지 한 달 만에 열여덟 번째 생일을 맞았다고 해요. 견습 기사가 된 뒤 한 달만 지났다면 열여덟 살이 될 수 없어서 두 시점의 나이가 함께 성립하지 않아요. 그 사이에 시간이 더 흘렀거나 나이가 달라질 이유는 원고에 나오지 않아요.
  - [expect:tower-count] (HIGH) “그날 밤 성벽 위 다섯 개의 망루마다 차례로 횃불이 올랐다. 망루는 그것이 전부였다.” ← c1: “회색 성의 성벽에는 망루가 모두 일곱 개 있었다.”
    1화 「견습 기사」에서는 회색 성의 성벽에 망루가 모두 일곱 개라고 정해 두었어요. 그런데 3화에서 그날 밤 횃불이 오른 망루는 다섯 개뿐이고, 그것이 전부라고 서술해요. 성벽의 망루가 일곱 개이면서 동시에 다섯 개가 전부일 수는 없어요. 그사이에 망루가 사라졌거나 다섯 개만 남은 까닭은 원고에 나오지 않아요.
- run 3: 2 found, 0 unverified, 2 kept, 30.1s
  - [expect:age-jump] (HIGH) “견습 기사가 된 지 한 달 만에, 에단은 열여덟 번째 생일을 맞았다.” ← c1: “에단은 열여섯 살 봄에 견습 기사가 되었다.”
    1화에서는 에단이 열여섯 살 봄에 견습 기사가 되었다고 정해요. 그런데 3화에서는 견습 기사가 된 지 한 달 만에 열여덟 번째 생일을 맞았다고 해요. 한 달 사이에 열여섯 살에서 열여덟 살이 될 수는 없고, 그 차이를 설명할 사건이나 이유도 원고에 나오지 않아요.
  - [expect:tower-count] (HIGH) “망루는 그것이 전부였다.” ← c1: “회색 성의 성벽에는 망루가 모두 일곱 개 있었다.”
    1화 「견습 기사」에서는 회색 성의 성벽에 망루가 일곱 개 있다고 정해 두었어요. 그런데 3화에서는 횃불을 올린 다섯 개가 망루의 전부라고 해요. 두 내용은 같은 회색 성의 망루 수를 일곱 개와 다섯 개로 달리 말해 함께 성립할 수 없어요. 1화 이후 망루가 사라지거나 수가 줄었다는 사건은 나오지 않아요.

### dead-man · 죽은 인물, 회상과 꿈
- run 1: 1 found, 0 unverified, 1 kept, 26.7s
  - [expect:alive-again] (HIGH) “아침이 되자 베른은 여느 때처럼 대장간 앞 의자에 앉아 지나가는 아이들에게 손을 흔들었다.” ← c1: “겨울 끝자락에 베른은 숨을 거두었다.”
    1화 「겨울 끝」에서는 베른이 겨울 끝자락에 숨을 거뒀다고 정해 두었어요. 그런데 2화 아침에는 베른이 대장간 앞 의자에 앉아 아이들에게 손을 흔들어요. 죽은 베른이 같은 시점에 살아 있는 모습으로 나타날 수는 없고, 2화에는 그가 되살아났거나 아침 장면도 꿈이라는 설명이 없어요.
- run 2: 1 found, 0 unverified, 0 kept, 31.3s; judge dropped: alive-again→INSUFFICIENT
- run 3: 1 found, 0 unverified, 1 kept, 29.3s
  - [expect:alive-again] (HIGH) “아침이 되자 베른은 여느 때처럼 대장간 앞 의자에 앉아 지나가는 아이들에게 손을 흔들었다.” ← c1: “겨울 끝자락에 베른은 숨을 거두었다.”
    1화 「겨울 끝」에서는 베른이 숨을 거두고 언덕 위 참나무 아래에 묻혔다고 정해 두었어요. 그런데 2화에서는 아침에 베른이 대장간 앞 의자에 앉아 아이들에게 손을 흔들어요. 죽어 묻힌 베른이 그 아침에 살아서 그 자리에 앉아 있을 수는 없어요. 1화 뒤로 베른이 되살아났거나 다른 이유로 나타났다는 장면이나 설명이 없어 두 내용이 함께 성립하지 않아요.

### alias-family · 별칭으로 부른 인물의 가족 관계
- run 1: 1 found, 0 unverified, 0 kept, 28.0s; judge dropped: brother-grave→INSUFFICIENT
- run 2: 1 found, 0 unverified, 1 kept, 31.9s
  - [expect:brother-grave] (HIGH) “비 오는 아침, 검은 백작은 남동생의 무덤 앞에 흰 꽃을 놓고 오래 서 있었다.” ← sera: “블랙우드 가의 외동딸. 형제자매가 없다.”
    「세라 블랙우드」 설정은 세라가 블랙우드 가의 외동딸이며 형제자매가 없다고 정해 두었어요. 그런데 2화에서 검은 백작인 세라가 남동생의 무덤 앞에 서 있는 것으로 서술돼요. 세라에게 남동생이 있다면 설정의 내용과 양립할 수 없고, 두 내용이 함께 성립하도록 형제 관계가 바뀌었거나 다른 사정이 생겼다는 장면도 나오지 않아요.
- run 3: 1 found, 0 unverified, 1 kept, 37.3s
  - [expect:brother-grave] (HIGH) “비 오는 아침, 검은 백작은 남동생의 무덤 앞에 흰 꽃을 놓고 오래 서 있었다.” ← sera: “형제자매가 없다.”
    「세라 블랙우드」 설정은 세라가 블랙우드 가의 외동딸이며 형제자매가 없다고 정해 두었어요. 그런데 2화에서는 검은 백작이 남동생의 무덤에 흰 꽃을 놓았다고 서술하고, 설정상 검은 백작은 세라의 별칭이에요. 따라서 그 무덤의 남동생이 세라의 동생이라면 두 내용은 함께 성립할 수 없어요. 세라에게 남동생이 생겼거나 설정이 바뀌었다고 볼 사건이나 설명은 제시되지 않아요.

### lighthouse-place · 장소의 위치와 규칙
- run 1: 2 found, 1 unverified, 1 kept, 34.2s
  - [expect:light-at-two] (HIGH) “새벽 두 시, 흰 등대의 불은 여전히 환하게 타오르고 있었다.” ← lighthouse: “등대의 불은 매일 밤 자정에 꺼진다.”
    ‘흰 등대’ 설정은 등대의 불이 매일 밤 자정에 꺼진다고 정해 두었어요. 그런데 2화에서는 새벽 두 시에도 불이 환하게 타오른다고 보여 줘요. 자정이 지난 뒤인데도 불이 켜져 있으므로 두 내용은 그 시점에 함께 성립할 수 없어요. 자정에 불이 다시 켜졌다는 사건이나 이유는 나오지 않아요.
- run 2: 3 found, 1 unverified, 2 kept, 38.0s
  - [expect:light-at-two] (HIGH) “새벽 두 시, 흰 등대의 불은 여전히 환하게 타오르고 있었다.” ← lighthouse: “등대의 불은 매일 밤 자정에 꺼진다.”
    〈흰 등대〉 설정은 등대의 불이 매일 밤 자정에 꺼진다고 정해 두었어요. 그런데 2화에서 새벽 두 시에도 흰 등대의 불이 환하게 타오른다고 나와요. 자정에 꺼진 불이 새벽 두 시까지 계속 타오를 수는 없고, 그 사이에 다시 불을 켰다는 사건이나 이유도 제시되지 않아요.
  - [expect:west-cliff] (HIGH) “노을이 지는 쪽, 항구 서쪽 끝 절벽 위의 흰 등대로 노아는 걸음을 옮겼다.” ← lighthouse: “항구 동쪽 끝 절벽 위에 있다.”
    ‘흰 등대’ 설정은 등대가 항구 동쪽 끝 절벽 위에 있다고 정해 두었어요. 그런데 2화에서 노아는 항구 서쪽 끝 절벽 위의 흰 등대로 걸음을 옮겨요. 같은 흰 등대가 항구의 동쪽 끝과 서쪽 끝에 동시에 있을 수 없고, 그 위치가 바뀌었거나 서쪽에 다른 등대가 있다는 내용도 나오지 않아요.
- run 3: 3 found, 1 unverified, 2 kept, 38.5s
  - [expect:light-at-two] (HIGH) “새벽 두 시, 흰 등대의 불은 여전히 환하게 타오르고 있었다.” ← lighthouse: “등대의 불은 매일 밤 자정에 꺼진다.”
    「흰 등대」 설정은 등대의 불이 매일 밤 자정에 꺼진다고 정해 두었어요. 그런데 2화에서 새벽 두 시에도 흰 등대의 불이 환하게 타오르고 있어요. 자정에 꺼진 불이 두 시간 뒤 다시 켜졌다는 설명이나 장면이 없어서, 두 내용은 함께 성립할 수 없어요.
  - [expect:west-cliff] (HIGH) “노을이 지는 쪽, 항구 서쪽 끝 절벽 위의 흰 등대로 노아는 걸음을 옮겼다.” ← lighthouse: “항구 동쪽 끝 절벽 위에 있다.”
    ‘흰 등대’ 설정은 등대가 항구 동쪽 끝 절벽 위에 있다고 정해 두었어요. 그런데 2화에서 노아는 항구 서쪽 끝 절벽 위의 흰 등대로 걸어가요. 같은 등대가 항구의 동쪽 끝과 서쪽 끝에 동시에 있을 수는 없고, 위치가 바뀌었거나 서쪽 끝에도 등대가 있다는 내용은 원고에 나오지 않아요.

### appearance · 외모와 설정에 없는 내용
- run 1: 1 found, 0 unverified, 1 kept, 39.3s
  - [expect:long-silver-hair] (HIGH) “허리까지 내려오는 은발을 쓸어 넘기며 유나가 웃었다.” ← yuna: “열일곱 살 고등학생. 검은 단발머리.”
    「유나」 설정은 유나가 검은 단발머리인 열일곱 살 고등학생이라고 정해 두었어요. 그런데 2화에서는 유나의 머리가 허리까지 내려오는 은발이라고 나와요. 머리색과 길이가 모두 달라 두 묘사는 함께 성립할 수 없고, 머리를 염색하거나 길게 바꾸었다는 설명도 원고에 없어요.
- run 2: 1 found, 0 unverified, 1 kept, 37.7s
  - [expect:long-silver-hair] (HIGH) “허리까지 내려오는 은발을 쓸어 넘기며 유나가 웃었다.” ← yuna: “열일곱 살 고등학생. 검은 단발머리.”
    ‘유나’ 인물 설정은 유나가 검은 단발머리라고 정해 두었어요. 그런데 2화에서 유나는 허리까지 내려오는 은발로 묘사돼요. 같은 시점의 유나가 검은 단발머리이면서 허리까지 내려오는 은발일 수는 없고, 머리색이나 길이가 달라진 까닭도 원고에 나오지 않아요.
- run 3: 1 found, 0 unverified, 1 kept, 40.3s
  - [expect:long-silver-hair] (HIGH) “허리까지 내려오는 은발을 쓸어 넘기며 유나가 웃었다.” ← yuna: “열일곱 살 고등학생. 검은 단발머리.”
    ‘유나’ 설정은 유나가 검은 단발머리인 열일곱 살 학생이라고 정해 두었어요. 그런데 2화 옥상 장면에서 유나는 허리까지 내려오는 은발을 쓸어 넘겨요. 검은 단발과 허리까지 오는 은발은 같은 시점의 머리 모양과 색으로 함께 성립할 수 없어요. 머리색이나 길이가 바뀌었다는 계기나 설명은 제시되지 않았어요.

### clean-exception · 규칙의 예외를 지킨 깨끗한 회차
- run 1: 0 found, 0 unverified, 0 kept, 39.1s
- run 2: 0 found, 0 unverified, 0 kept, 39.2s
- run 3: 0 found, 0 unverified, 0 kept, 39.6s

### seal-explained · 회차 안에서 풀린 모순(왕족의 피)
- run 1: 0 found, 0 unverified, 0 kept, 40.7s
- run 2: 0 found, 0 unverified, 0 kept, 41.7s
- run 3: 0 found, 0 unverified, 0 kept, 41.7s

### seal-unexplained · 풀리지 않은 모순(평민이 봉인을 푼다)
- run 1: 1 found, 0 unverified, 0 kept, 48.2s; judge dropped: commoner-seal→INSUFFICIENT
- run 2: 1 found, 0 unverified, 1 kept, 47.2s
  - [expect:commoner-seal] (HIGH) “테스는 신전 제단에 손을 대는 것만으로 봉인을 풀고 왕의 검을 뽑아 들었다.” ← seal: “북쪽 신전의 봉인은 왕족의 피로만 풀린다.”
    ‘북쪽 신전의 봉인’ 설정은 봉인이 왕족의 피로만 풀린다고 정해 두었어요. 그런데 2화에서는 테스가 제단에 손을 대는 것만으로 봉인을 풀고 왕의 검을 뽑아요. 테스가 왕족의 피를 지녔다는 내용이나 다른 해제 원인이 제시되지 않아, 두 내용은 함께 성립할 수 없어요.
- run 3: 1 found, 0 unverified, 0 kept, 46.6s; judge dropped: commoner-seal→INSUFFICIENT

### english-letters · English: reading and pretending
- run 1: 1 found, 0 unverified, 1 kept, 46.2s
  - [expect:reads-aloud] (HIGH) “When the courier left, Mira read the sealed letter aloud to the whole tavern, word by word.” ← mira-en: “A tavern girl who cannot read.”
    The “Mira” character document establishes that Mira cannot read and was never taught letters. In Chapter 2, she reads the courier’s sealed letter aloud, word by word. Those facts cannot both be true at that point, and the chapter gives no explanation for how she can read the letter.
- run 2: 1 found, 0 unverified, 1 kept, 47.3s
  - [expect:reads-aloud] (HIGH) “When the courier left, Mira read the sealed letter aloud to the whole tavern, word by word.” ← mira-en: “A tavern girl who cannot read.”
    The Mira character document establishes that she cannot read and was never taught letters. In Chapter 2, after the courier leaves, she reads the sealed letter aloud to the whole tavern, word by word. Those facts cannot both be true at that point, and the chapter shows no reason she can read the letter despite the established inability.
- run 3: 1 found, 0 unverified, 1 kept, 47.9s
  - [expect:reads-aloud] (HIGH) “When the courier left, Mira read the sealed letter aloud to the whole tavern, word by word.” ← mira-en: “A tavern girl who cannot read.”
    The “Mira” character document establishes that Mira cannot read and was never taught her letters. In Chapter 2, she reads the sealed letter aloud word by word to the tavern. Those facts cannot both hold at this point, and the chapter gives no reason she can read the letter despite the established setting.

### glass-garden-8-sea · 장편: 바다를 두려워하는 레나가 배에 오른다
- run 1: 1 found, 0 unverified, 0 kept, 60.7s; judge dropped: lena-boards→CHANGED_OVER_TIME
- run 2: 1 found, 0 unverified, 1 kept, 53.6s
  - [expect:lena-boards] (HIGH) “레나는 은빛 항구에서 작은 배를 타고 처음으로 바다에 나갔다.” ← lena: “바다를 몹시 두려워해 배에 오르지 않는다.”
    ‘레나 아르벨’ 설정은 레나가 바다를 몹시 두려워해 배에 오르지 않는다고 정해 두었어요. 그런데 8화에서는 레나가 은빛 항구에서 작은 배를 타고 바다에 나가요. 배를 타지 않는다는 설정과 실제 승선 장면은 같은 시점에 함께 성립할 수 없어요. 레나가 두려움을 극복했거나 마음을 바꾸게 된 계기는 원고에 나오지 않아요.
- run 3: 1 found, 0 unverified, 1 kept, 62.3s
  - [expect:lena-boards] (HIGH) “레나는 은빛 항구에서 작은 배를 타고 처음으로 바다에 나갔다.” ← lena: “바다를 몹시 두려워해 배에 오르지 않는다.”
    ‘레나 아르벨’ 설정은 레나가 바다를 몹시 두려워해 배에 오르지 않는다고 정해 두었어요. 그런데 8화에서는 서윤이 지켜보는 가운데 레나가 작은 배에 올라 처음 바다로 나가요. 배를 탈 수 없다는 설정과 실제로 배를 타는 행동은 이 시점에 함께 성립할 수 없어요. 레나가 두려움을 극복하게 된 계기나 배에 오를 수밖에 없는 사정은 원고에 나오지 않아요.

### spring-table-18-years · 장편: 태오의 근무 연수 10년과 5년
- run 1: 1 found, 0 unverified, 1 kept, 55.0s
  - [expect:five-years] (HIGH) “태오는 다른 식당의 스카우트 제안을 받았지만 봄날에서 5년을 함께했다며 거절했다.” ← teo: “봄날에서 10년째 일했다.”
    인물 설정 「박태오」는 태오가 봄날에서 10년째 일했다고 정해 두었어요. 그런데 18화에서는 스카우트를 거절하는 이유로 봄날에서 5년을 함께했다고 서술해요. 같은 시점에 태오의 근무 기간이 10년째이면서 5년이라고 할 수는 없고, 그 사이 근무 기간이 달라졌거나 이 말을 설명할 사정도 나오지 않아요.
- run 2: 1 found, 0 unverified, 1 kept, 56.0s
  - [expect:five-years] (HIGH) “태오는 다른 식당의 스카우트 제안을 받았지만 봄날에서 5년을 함께했다며 거절했다.” ← teo: “봄날의 요리사. 봄날에서 10년째 일했다.”
    ‘박태오’ 설정은 태오가 봄날에서 10년째 일했다고 정해 두었어요. 그런데 18화에서는 스카우트 제안을 거절한 이유로 봄날에서 5년을 함께했다고 말해요. 같은 시점의 태오가 봄날에서 일한 기간이 10년째이면서 5년일 수는 없고, 그 사이에 기간을 다르게 설명할 만한 사건이나 이유도 나오지 않아요.
- run 3: 1 found, 0 unverified, 1 kept, 55.8s
  - [expect:five-years] (HIGH) “태오는 다른 식당의 스카우트 제안을 받았지만 봄날에서 5년을 함께했다며 거절했다.” ← teo: “봄날의 요리사. 봄날에서 10년째 일했다.”
    ‘박태오’ 설정은 태오가 봄날에서 10년째 일했다고 정해 두었어요. 그런데 18화에서 태오는 스카우트 제안을 거절하며 봄날에서 함께한 시간이 5년이라고 말해요. 두 기간은 이 장면에서 함께 성립할 수 없고, 태오가 봄날을 떠났다가 돌아왔거나 근속 기간을 다르게 계산한다는 내용도 나오지 않아요.

### fog-harbor-13-tea · 장편: 차를 마시지 않는 선장의 독 찻잔
- run 1: 1 found, 0 unverified, 0 kept, 60.5s; judge dropped: teacup→INSUFFICIENT
- run 2: 1 found, 0 unverified, 0 kept, 64.7s; judge dropped: teacup→INSUFFICIENT
- run 3: 1 found, 0 unverified, 0 kept, 64.9s; judge dropped: teacup→INSUFFICIENT

