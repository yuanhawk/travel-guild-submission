// @vitest-environment jsdom
//
// COMPONENT test — #225 CJK lane.
//
// #168/#181/#222 wired namePresentation() into every raw-name render site in
// Itinerary.svelte (leg-header hotel name, scheduled activities/meals, the
// meal-swap pool, unscheduled suggestions, and hotel-endpoint day-end hops),
// but every one of those fixes was live-reproduced and regression-tested with
// EITHER a Japanese-kanji placeholder OR Arabic (Dubai, #222) — Korean Hangul
// (a distinct Unicode block from Han/Kana/Arabic) and Chinese hanzi in a
// second, non-Japanese country context had never been proven end-to-end
// through the component. Every string below is copied verbatim from a real
// live /negotiate_text response (Seoul, Beijing — local LLM-off orchestrator,
// current checkout), not a synthetic placeholder.

import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import Itinerary from './components/Itinerary.svelte';
import type { DayPlan, Leg } from './api';

const UNREADABLE_TEXT = 'shown in original script — no English name available';

describe('#225 CJK lane: Korean (Hangul) real live strings', () => {
  const LEGS = [{ leg_id: 'leg-0', city: 'Seoul' }] as unknown as Leg[];

  it('a scheduled meal with only a Hangul name (no name_en) shows the unreadable indicator', () => {
    const plans = [{
      leg_id: 'leg-0', city: 'Seoul', country: 'South Korea', num_days: 1,
      days: [{
        day_index: 0, bad_weather: false, attractions: [],
        // Real live string from a Seoul run: pure Hangul, no name_en harvested.
        meals: { lunch: { name: '그로밋 커피하우스', category: 'restaurant', cuisine: 'coffee_shop' } },
      }],
    }] as unknown as DayPlan[];
    const { getByText } = render(Itinerary, { props: { plans, legs: LEGS, idempotencyKey: '' } });
    expect(getByText('그로밋 커피하우스')).toBeTruthy();
    expect(getByText(UNREADABLE_TEXT)).toBeTruthy();
  });

  it('a scheduled meal with a Hangul name PLUS name_en shows the English primary + local companion, no unreadable indicator', () => {
    const plans = [{
      leg_id: 'leg-0', city: 'Seoul', country: 'South Korea', num_days: 1,
      days: [{
        day_index: 0, bad_weather: false, attractions: [],
        // Real live pair from a Seoul run.
        meals: { lunch: { name: '7번가 피자', name_en: '7th Street pizza', category: 'restaurant', cuisine: 'pizza' } },
      }],
    }] as unknown as DayPlan[];
    const { getByText, queryByText } = render(Itinerary, { props: { plans, legs: LEGS, idempotencyKey: '' } });
    expect(getByText('7th Street pizza')).toBeTruthy();
    expect(getByText('7번가 피자')).toBeTruthy();
    expect(queryByText(UNREADABLE_TEXT)).toBeNull();
  });

  it('a Hangul-only lodging name (leg.hotel_title) shows the unreadable indicator', () => {
    const plans = [{
      leg_id: 'leg-0', city: 'Seoul', country: 'South Korea', num_days: 1,
      days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
    }] as unknown as DayPlan[];
    const legs = [{ leg_id: 'leg-0', city: 'Seoul', hotel_title: '그랜드 하얏트 서울' }] as unknown as Leg[];
    const { getByTestId, getByText } = render(Itinerary, { props: { plans, legs, idempotencyKey: '' } });
    const hotelName = getByTestId('hotel-name-0');
    expect(hotelName.textContent).toContain('그랜드 하얏트 서울');
    expect(getByText(UNREADABLE_TEXT)).toBeTruthy();
  });
});

describe('#225 CJK lane: Chinese (simplified hanzi) real live strings', () => {
  const LEGS = [{ leg_id: 'leg-0', city: 'Beijing' }] as unknown as Leg[];

  it('a scheduled meal with only a hanzi name (no name_en) shows the unreadable indicator', () => {
    const plans = [{
      leg_id: 'leg-0', city: 'Beijing', country: 'China', num_days: 1,
      days: [{
        day_index: 0, bad_weather: false, attractions: [],
        // Real live string from a Beijing run: no name_en harvested.
        meals: { tea: { name: '海淀第一深情之家', category: 'restaurant', cuisine: 'cafe' } },
      }],
    }] as unknown as DayPlan[];
    const { getByText } = render(Itinerary, { props: { plans, legs: LEGS, idempotencyKey: '' } });
    expect(getByText('海淀第一深情之家')).toBeTruthy();
    expect(getByText(UNREADABLE_TEXT)).toBeTruthy();
  });

  it('a scheduled meal with a hanzi name PLUS name_en shows the English primary + local companion, no unreadable indicator', () => {
    const plans = [{
      leg_id: 'leg-0', city: 'Beijing', country: 'China', num_days: 1,
      days: [{
        day_index: 0, bad_weather: false, attractions: [],
        // Real live pair from a Beijing run.
        meals: { breakfast: { name: '星巴克', name_en: 'Starbucks', category: 'restaurant', cuisine: 'coffee_shop' } },
      }],
    }] as unknown as DayPlan[];
    const { getByText, queryByText } = render(Itinerary, { props: { plans, legs: LEGS, idempotencyKey: '' } });
    expect(getByText('Starbucks')).toBeTruthy();
    expect(getByText('星巴克')).toBeTruthy();
    expect(queryByText(UNREADABLE_TEXT)).toBeNull();
  });

  it('an unscheduled attraction suggestion with only a hanzi name shows the unreadable indicator', () => {
    const plans = [{
      leg_id: 'leg-0', city: 'Beijing', country: 'China', num_days: 1,
      days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
      // Real live string from a Beijing run's unscheduled_attractions pool.
      unscheduled_attractions: [{ name: '中国铁道博物馆（正阳门展馆）', category: 'tourism=museum', lat: 39.9, lon: 116.4 }],
    }] as unknown as DayPlan[];
    const { getByText } = render(Itinerary, { props: { plans, legs: LEGS, idempotencyKey: '', editable: true } });
    expect(getByText('中国铁道博物馆（正阳门展馆）')).toBeTruthy();
    expect(getByText(UNREADABLE_TEXT)).toBeTruthy();
  });
});
