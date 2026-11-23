"""Protocol constants. Every node on a network must use the same values."""

# Economics (whole NYX units, no decimals)
BLOCK_REWARD = 10
MIN_FEE = 1
MIN_STAKE = 100
UNBOND_BLOCKS = 10        # unstaked coins stay locked (and slashable) for this many blocks

# Reputation = recent reliability, in basis points (10000 = 100%).
# Every duty a validator has (vote on time, propose when chosen) is one sample in an
# exponentially weighted moving average: rep <- rep * d + 100% * (1 - d) * outcome.
# With d = 0.9862 a sample loses half its influence after 50 newer samples (most validators
# have one duty per block, so roughly 50 blocks; proposing and missed slots add samples).
# Old work fades, and validators doing the same work converge to the same score.
# Integers keep every node's arithmetic identical, which floats would not guarantee.
REP_MAX = 10_000
REP_MIN = 500             # floor for non-slashed validators, so a recovered node can vote and earn back trust
REP_GENESIS = 5_000       # genesis validators start at 50%
REP_NEW = 1_000           # validators who stake later start at 10% and must earn the rest
REP_DECAY_NUM = 9_862     # d = 0.9862: half-life of about 50 duties (about 50 blocks)
REP_DECAY_DEN = 10_000
REP_HALF_LIFE = 50        # documentation only; follows from d above
VOTE_DEADLINE = 3         # a vote for block h must be on chain by block h + 3 to count as on time
MISSED_SLOT_SAMPLES = 5   # a missed block costs the whole network a slot, so it counts as 5 failed duties

# Voting-weight cap: the largest validators are pulled down so none holds more than 30% of
# total voting weight. Finality needs 2/3, so it stalls only if more than 1/3 of weight is
# offline: below 1/3, no single validator can stall it or come close to finalizing alone.
# Capping a big validator raises everyone else's share, so the cap is limited: it may never
# more than double any other validator's share (MAX_BOOST). Tiny or low-reputation validators
# can therefore never be handed a large share. A single validator is held to 30% as long as it
# owns under about 65% of all raw weight; beyond that the cap still cuts it, but less.
# Needs 4+ validators.
MAX_SHARE_NUM = 3
MAX_SHARE_DEN = 10
MAX_BOOST = 2

# Finality needs 2/3, so it stalls only if more than 1/3 of weight is offline. Keeping every
# validator below 1/3 means no one, however rich, can stall finality alone or come close
# to finalizing alone. Applies when it can be met (4 or more validators).
MAX_SHARE_NUM = 3
MAX_SHARE_DEN = 10

# Finality
FINALITY_NUM = 2          # a block is final once votes from >= 2/3 of total weight are on chain
FINALITY_DEN = 3
VOTE_WINDOW = 64          # votes may target blocks at most this many heights back

# Slashing
SLASH_NUM = 1             # an equivocating validator loses 1/2 of its stake and unbonding coins
SLASH_DEN = 2
WHISTLEBLOWER_NUM = 1     # the proposer that includes the evidence gets 1/10 of the slashed amount
WHISTLEBLOWER_DEN = 10

# Blocks
MAX_TXS_PER_BLOCK = 500
MAX_VOTES_PER_BLOCK = 256
MAX_EVIDENCE_PER_BLOCK = 8
MAX_MISSED_SCAN = 1_000   # cap on skipped slots penalised by one block
STATE_HISTORY = 200       # keep post-states this many blocks back
