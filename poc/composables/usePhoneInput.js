// Phone-number entry for the auth screen (ADR 0009): a worldwide country-code
// picker + a lightweight E.164 validator. Deliberately NOT libphonenumber-js
// (bundle size, no build step) - the regex + a per-country length hint is
// enough to stop typos, not to be authoritative.
//
// Dev whitelist: a raw input of exactly "1".."5" is a non-real test number that
// bypasses validation and routes through the legacy OTP stub. Everything else
// must be a valid E.164 number and goes through Firebase SMS.
function usePhoneInput(ctx) {
  const { ref, computed } = Vue;

  const COUNTRIES = window.COUNTRY_CODES || [];
  const NSN_LEN = window.COUNTRY_NSN_LEN || {};
  const DEV_WHITELIST = new Set(['1', '2', '3', '4', '5']);

  function guessCountry() {
    const region = (navigator.language || '').split('-')[1];
    if (region) {
      const hit = COUNTRIES.find((c) => c.iso2 === region.toUpperCase());
      if (hit) return hit;
    }
    return COUNTRIES.find((c) => c.iso2 === 'IL') || COUNTRIES[0] || { iso2: 'IL', name: 'Israel', dial: '972', flag: '' };
  }

  const country = ref(guessCountry());
  // Raw string exactly as typed - used for the whitelist check.
  const rawInput = ref('');

  // Digits only, leading zero(s) stripped (national trunk prefix).
  const nationalDigits = computed(() => rawInput.value.replace(/\D/g, '').replace(/^0+/, ''));

  const e164 = computed(() => '+' + country.value.dial + nationalDigits.value);

  const isWhitelisted = computed(() => DEV_WHITELIST.has(rawInput.value.trim()));

  const isValidPhone = computed(() => {
    if (isWhitelisted.value) return true;
    const nsn = nationalDigits.value;
    if (!nsn) return false;
    if (!/^\+[1-9]\d{7,14}$/.test(e164.value)) return false;
    const hint = NSN_LEN[country.value.iso2];
    if (hint) return nsn.length >= hint[0] && nsn.length <= hint[1];
    return nsn.length >= 6 && nsn.length <= 14;
  });

  // What the rest of the app treats as "the phone number": the raw 1..5 token
  // for whitelisted entries, otherwise the resolved E.164 string.
  const resolvedPhone = computed(() => (isWhitelisted.value ? rawInput.value.trim() : e164.value));

  function resetPhoneInput() {
    country.value = guessCountry();
    rawInput.value = '';
  }

  return {
    phoneCountries: COUNTRIES,
    phoneCountry: country,
    phoneRawInput: rawInput,
    phoneE164: e164,
    phoneIsWhitelisted: isWhitelisted,
    phoneIsValid: isValidPhone,
    resolvedPhone,
    resetPhoneInput,
  };
}
