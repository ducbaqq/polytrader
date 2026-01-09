// Generate realistic golf statistics payload
// Based on official PGA Tour statistics

interface RoundStats {
  double_eagles_or_better: number;
  eagles: number;
  birdies: number;
  pars: number;
  bogeys: number;
  double_bogeys: number;
  triple_bogeys_or_worse: number;
  hole_in_ones: number;
  strokes: number;
  round: number;
  score: number;
  thru: number;
}

interface ActivityAttributes {
  double_eagles_or_better: number;
  eagles: number;
  birdies: number;
  pars: number;
  bogeys: number;
  double_bogeys: number;
  triple_bogeys_or_worse: number;
  hole_in_ones: number;
  strokes: number;
  round: number;
  score: number;
  thru: number;
  position: number;
  tied: boolean;
  money: number;
  points: number;
  rounds: RoundStats[];
  player_holes_remaining: number;
  exit_strokes_subtraction: number;
}

interface PlayerPayload {
  id: string;
  gameId: string;
  activityAttributes: ActivityAttributes;
  activityGameUnitsRemaining: number;
}

// Realistic golf statistics ranges based on PGA Tour averages
function generateRoundStats(roundNumber: number): RoundStats {
  const par = 72; // Standard par for 18 holes
  
  // Generate realistic hole-by-hole outcomes
  // Professional golfers typically:
  // - Birdies: 2-5 per round
  // - Pars: 10-14 per round (most common)
  // - Bogeys: 2-5 per round
  // - Double bogeys: 0-2 per round
  // - Triple+: 0-1 per round
  // - Eagles: 0-1 per round (rare)
  // - Double eagles: 0 per round (extremely rare)
  
  const birdies = Math.floor(Math.random() * 4) + 2; // 2-5
  const pars = Math.floor(Math.random() * 5) + 10; // 10-14
  const bogeys = Math.floor(Math.random() * 4) + 2; // 2-5
  const doubleBogeys = Math.random() < 0.3 ? Math.floor(Math.random() * 2) : 0; // 0-1, 30% chance
  const tripleBogeys = Math.random() < 0.1 ? 1 : 0; // 0-1, 10% chance
  const eagles = Math.random() < 0.2 ? 1 : 0; // 0-1, 20% chance
  const doubleEagles = 0; // Extremely rare
  const holeInOnes = 0; // Extremely rare
  
  // Calculate strokes: par is 72, adjust based on outcomes
  // Each birdie saves 1 stroke, each bogey adds 1, etc.
  const strokes = par + bogeys + (doubleBogeys * 2) + (tripleBogeys * 3) - birdies - (eagles * 2);
  const score = strokes - par; // Score relative to par
  
  return {
    double_eagles_or_better: doubleEagles,
    eagles: eagles,
    birdies: birdies,
    pars: pars,
    bogeys: bogeys,
    double_bogeys: doubleBogeys,
    triple_bogeys_or_worse: tripleBogeys,
    hole_in_ones: holeInOnes,
    strokes: strokes,
    round: roundNumber,
    score: score,
    thru: 18
  };
}

function generatePlayerPayload(playerId: string, gameId: string): PlayerPayload {
  const rounds: RoundStats[] = [];
  let totalStrokes = 0;
  let totalBirdies = 0;
  let totalPars = 0;
  let totalBogeys = 0;
  let totalDoubleBogeys = 0;
  let totalTripleBogeys = 0;
  let totalEagles = 0;
  let totalDoubleEagles = 0;
  let totalHoleInOnes = 0;
  
  // Generate 4 rounds (typical tournament)
  for (let round = 1; round <= 4; round++) {
    const roundStats = generateRoundStats(round);
    rounds.push(roundStats);
    
    totalStrokes += roundStats.strokes;
    totalBirdies += roundStats.birdies;
    totalPars += roundStats.pars;
    totalBogeys += roundStats.bogeys;
    totalDoubleBogeys += roundStats.double_bogeys;
    totalTripleBogeys += roundStats.triple_bogeys_or_worse;
    totalEagles += roundStats.eagles;
    totalDoubleEagles += roundStats.double_eagles_or_better;
    totalHoleInOnes += roundStats.hole_in_ones;
  }
  
  const totalPar = 72 * 4; // 288 for 4 rounds
  const totalScore = totalStrokes - totalPar;
  
  // Generate position (1-100+)
  const position = Math.floor(Math.random() * 100) + 1;
  const tied = Math.random() < 0.15; // 15% chance of being tied
  
  // Generate money based on position (rough estimate for PGA Tour)
  // Top positions get more money
  let money = 0;
  if (position <= 10) {
    money = Math.floor(Math.random() * 500000) + 500000; // $500k-$1M
  } else if (position <= 25) {
    money = Math.floor(Math.random() * 200000) + 200000; // $200k-$400k
  } else if (position <= 50) {
    money = Math.floor(Math.random() * 100000) + 50000; // $50k-$150k
  } else {
    money = Math.floor(Math.random() * 50000) + 10000; // $10k-$60k
  }
  
  // Generate points (FedEx Cup points or similar)
  const points = Math.floor(Math.random() * 500) + 1;
  
  // Randomize activityGameUnitsRemaining (seems like time/energy units)
  const activityGameUnitsRemaining = Math.floor(Math.random() * 10000) + 1000; // 1000-11000
  
  return {
    id: playerId,
    gameId: gameId,
    activityAttributes: {
      double_eagles_or_better: totalDoubleEagles,
      eagles: totalEagles,
      birdies: totalBirdies,
      pars: totalPars,
      bogeys: totalBogeys,
      double_bogeys: totalDoubleBogeys,
      triple_bogeys_or_worse: totalTripleBogeys,
      hole_in_ones: totalHoleInOnes,
      strokes: totalStrokes,
      round: 4,
      score: totalScore,
      thru: 72,
      position: position,
      tied: tied,
      money: money,
      points: points,
      rounds: rounds,
      player_holes_remaining: 0,
      exit_strokes_subtraction: 0
    },
    activityGameUnitsRemaining: activityGameUnitsRemaining
  };
}

// Player IDs from the file
const playerIds = [
  "1659111c-c962-4b55-a966-770ad40b2c21",
  "76175ddf-0160-49c5-b1eb-0c1a96c307d6",
  "661818e5-d406-44de-9e71-f8fb4c2a03ff",
  "a4e367d9-3eac-47b3-93cb-5246aafe6d3d",
  "f6aee2de-413e-4a1b-adf9-70b8ceff8ffa",
  "89bbdceb-d73d-4eda-9417-eb76074023d4",
  "521c9081-b737-4743-8b7d-771446dd85d7",
  "4e53261d-8f4e-4b93-8c80-96c997ff6490",
  "0922acf2-1efe-48af-bb93-36a5992a68d5",
  "6ae2080c-7935-4eac-9fc2-3d3d665df713",
  "cd753ced-1bf3-4d2a-bbba-741395863688",
  "ce7763fb-8aff-441c-8df7-10308a543670",
  "bc59308a-fbb3-402c-8b5d-17111b31376b",
  "2548222f-1339-4f98-853e-6adbeb6444af",
  "40e08248-0f1d-405a-b992-f5a992480563",
  "b2ce17ba-7679-4335-b46c-497d40deb221",
  "c554024e-d230-4b6e-a69f-51e9854b1f85",
  "ff402789-8af7-4d21-a451-df208b42fe23",
  "ecb0778c-0cbf-499f-a9ed-4ebd2ee1031c",
  "d4ec45f1-01de-46fd-b0d6-52f2db853244"
];

const gameId = "42be6fc9-8e3d-4852-b726-6bceacc90249";

// Generate payload array
const payload: PlayerPayload[] = playerIds.map(playerId => 
  generatePlayerPayload(playerId, gameId)
);

// Output the JSON
console.log(JSON.stringify(payload, null, 2));

