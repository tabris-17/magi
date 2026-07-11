/* Polaris — small shared UI bits for the journal pages (page.html + tags.html).
 *
 * POLARIS_EMOJI: a curated set of exactly 100 journal-flavoured emoji (planning/todo,
 * reminders, work, study, family, health, food, travel, money, hobbies, nature, moods,
 * pets) — a tag picks ONE as its icon. buildEmojiPicker() renders them as a grid of
 * buttons into a container and reports the pick; the caller owns the modal around it.
 */
(function (root) {
  'use strict';

  const POLARIS_EMOJI = [
    // planning · todo · reminders
    '✅', '📝', '📋', '🗒️', '📌', '📍', '🗓️', '📅', '⏰', '⏳', '🔔', '🎯',
    // work · office · computing
    '💼', '🏢', '👔', '🖥️', '💻', '⌨️', '📊', '📈', '📉', '🧾', '📎', '🗂️',
    // study · education · science
    '📚', '📖', '🎓', '✏️', '📐', '🧮', '🔬', '🧪', '🧠',
    // family · people · home
    '👨‍👩‍👧‍👦', '👶', '🧒', '💑', '❤️', '🏠', '🏡', '🎂', '🎁',
    // health · fitness
    '🏥', '💊', '🩺', '🏃', '🧘', '💪', '🛌', '🚴', '⚽',
    // food · drink
    '🍽️', '🍳', '☕', '🍵', '🍕',
    // travel · outdoors
    '✈️', '🚗', '🚆', '🗺️', '🧳', '🏖️', '⛰️', '🏕️', '🌍',
    // money · admin
    '💰', '💸', '🏦', '💳', '🏷️',
    // hobbies · leisure
    '🎨', '🎵', '🎸', '🎮', '📷', '🎬', '📺', '🎣', '🌱', '🪴',
    // nature · weather
    '🌞', '🌙', '⭐', '🌈', '🌧️', '❄️', '🍂', '🌸',
    // moods · sparks
    '😀', '😌', '😢', '😴', '🥳', '🤔', '💡', '🔑', '✨', '🔥',
    // pets
    '🐱', '🐶',
  ];

  /** Fill `container` with the emoji grid. onPick(emoji) gets '' for "no icon". */
  function buildEmojiPicker(container, onPick) {
    container.innerHTML = '';
    const none = document.createElement('button');
    none.type = 'button';
    none.className = 'pol-emoji-none';
    none.textContent = 'no icon';
    none.onclick = () => onPick('');
    container.appendChild(none);
    for (const e of POLARIS_EMOJI) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'pol-emoji-btn';
      b.textContent = e;
      b.onclick = () => onPick(e);
      container.appendChild(b);
    }
  }

  const api = { POLARIS_EMOJI, buildEmojiPicker };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof globalThis !== 'undefined' ? globalThis : this);
